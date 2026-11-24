// The game and its SQLite transactions stay in the authenticated web application.
// This process supplies an ordered WebRTC data channel for the loopback installation.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/pion/ice/v4"
	"github.com/pion/webrtc/v4"
)

const channelName = "elevator.v1"
const chunkSize = 12 * 1024
const maxReply = 4 << 20

type gateway struct {
	api    *webrtc.API
	game   string
	client *http.Client
	seats  chan struct{}
}

type signal struct {
	SDP     string `json:"sdp"`
	Session string `json:"session"`
}

type gameRequest struct {
	ID   uint32          `json:"id"`
	Op   string          `json:"op"`
	Base string          `json:"base"`
	Body json.RawMessage `json:"body"`
}

type replyHeader struct {
	ID     uint32 `json:"id"`
	Status int    `json:"status"`
	Bytes  int    `json:"bytes"`
}

var transportError = []byte(`{"error":{"code":"transport_error","message":"游戏回复未完整送达，请重新同步存档。"}}`)
var badRequest = []byte(`{"error":{"code":"invalid_request","message":"无效的游戏操作。"}}`)

func (g *gateway) play(ctx context.Context, session string, msg gameRequest) (int, []byte) {
	method := http.MethodPost
	switch msg.Op {
	case "ping":
		return 200, []byte(`{"ok":true}`)
	case "state":
		method = http.MethodGet
	case "action", "command":
		if len(msg.Body) == 0 || len(msg.Body) > 4096 {
			return 400, badRequest
		}
	default:
		return 400, badRequest
	}
	if len(msg.Base) > 64 || strings.ContainsAny(msg.Base, "\r\n") {
		return 400, badRequest
	}
	req, err := http.NewRequestWithContext(ctx, method, g.game+"/elevator/api/game/"+msg.Op, bytes.NewReader(msg.Body))
	if err != nil {
		return 502, transportError
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Origin", g.game)
	req.Header.Set("X-Elevator-Sync", "1")
	req.Header.Set("X-Elevator-Base", msg.Base)
	req.AddCookie(&http.Cookie{Name: "elevator_session", Value: session})
	res, err := g.client.Do(req)
	if err != nil {
		return 502, transportError
	}
	defer res.Body.Close()
	body, err := io.ReadAll(io.LimitReader(res.Body, maxReply+1))
	if err != nil || len(body) > maxReply || !json.Valid(body) {
		return 502, transportError
	}
	return res.StatusCode, body
}

func sendReply(dc *webrtc.DataChannel, id uint32, status int, body []byte) error {
	header, _ := json.Marshal(replyHeader{ID: id, Status: status, Bytes: len(body)})
	if err := dc.SendText(string(header)); err != nil {
		return err
	}
	deadline := time.Now().Add(12 * time.Second)
	for len(body) > 0 {
		for dc.BufferedAmount() > 256*1024 {
			if time.Now().After(deadline) || dc.ReadyState() != webrtc.DataChannelStateOpen {
				return errors.New("game channel stalled")
			}
			time.Sleep(5 * time.Millisecond)
		}
		n := min(chunkSize, len(body))
		if err := dc.Send(body[:n]); err != nil {
			return err
		}
		body = body[n:]
	}
	return nil
}

func (g *gateway) offer(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost || r.Header.Get("Origin") != "" || r.Header.Get("Content-Type") != "application/json" {
		http.Error(w, "invalid signalling request", http.StatusBadRequest)
		return
	}
	var s signal
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 48<<10)).Decode(&s); err != nil ||
		len(s.Session) < 16 || len(s.Session) > 256 || len(s.SDP) > 32<<10 ||
		strings.Count(s.SDP, "\nm=") != 1 || !strings.Contains(s.SDP, "\nm=application ") {
		http.Error(w, "invalid game offer", http.StatusBadRequest)
		return
	}
	select {
	case g.seats <- struct{}{}:
	default:
		http.Error(w, "busy", http.StatusServiceUnavailable)
		return
	}
	pc, err := g.api.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		<-g.seats
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	var once sync.Once
	closePeer := func() {
		once.Do(func() { cancel(); _ = pc.Close(); <-g.seats })
	}
	connectTimer := time.AfterFunc(20*time.Second, closePeer)
	lifetime := time.AfterFunc(30*time.Minute, closePeer)
	pc.OnConnectionStateChange(func(state webrtc.PeerConnectionState) {
		switch state {
		case webrtc.PeerConnectionStateFailed, webrtc.PeerConnectionStateClosed, webrtc.PeerConnectionStateDisconnected:
			connectTimer.Stop()
			lifetime.Stop()
			go closePeer()
		}
	})
	var used atomic.Bool
	pc.OnDataChannel(func(dc *webrtc.DataChannel) {
		if dc.Label() != channelName || !dc.Ordered() || dc.MaxRetransmits() != nil ||
			dc.MaxPacketLifeTime() != nil || !used.CompareAndSwap(false, true) {
			go closePeer()
			return
		}
		dc.OnOpen(func() { connectTimer.Stop() })
		dc.OnClose(func() { go closePeer() })
		// SCTP is ordered and Pion dispatches incoming messages serially.
		var lastID uint32
		dc.OnMessage(func(raw webrtc.DataChannelMessage) {
			var msg gameRequest
			if !raw.IsString || len(raw.Data) > 8192 || json.Unmarshal(raw.Data, &msg) != nil || msg.ID <= lastID {
				go closePeer()
				return
			}
			lastID = msg.ID
			status, body := g.play(ctx, s.Session, msg)
			if sendReply(dc, msg.ID, status, body) != nil {
				go closePeer()
			}
		})
	})
	err = pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeOffer, SDP: s.SDP})
	if err == nil {
		var answer webrtc.SessionDescription
		answer, err = pc.CreateAnswer(nil)
		if err == nil {
			gathered := webrtc.GatheringCompletePromise(pc)
			err = pc.SetLocalDescription(answer)
			if err == nil {
				select {
				case <-gathered:
					w.Header().Set("Content-Type", "application/json")
					if json.NewEncoder(w).Encode(pc.LocalDescription()) == nil {
						return
					}
				case <-r.Context().Done():
				case <-time.After(4 * time.Second):
				}
			}
		}
	}
	closePeer()
	http.Error(w, "could not negotiate game channel", http.StatusBadRequest)
}

func loopback(port int) string { return net.JoinHostPort("127.0.0.1", strconv.Itoa(port)) }

func main() {
	control := flag.Int("control-port", 8093, "local signalling port")
	game := flag.Int("game-port", 8092, "local authenticated game API port")
	udp := flag.Int("udp-port", 34780, "local game data port")
	flag.Parse()
	for _, port := range []int{*control, *game, *udp} {
		if port < 1 || port > 65535 {
			log.Fatal("invalid port")
		}
	}
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: *udp})
	if err != nil {
		log.Fatal(err)
	}
	mux := ice.NewUDPMuxDefault(ice.UDPMuxParams{UDPConn: conn})
	defer mux.Close()
	var settings webrtc.SettingEngine
	settings.SetNetworkTypes([]webrtc.NetworkType{webrtc.NetworkTypeUDP4})
	settings.SetLite(true)
	settings.SetIncludeLoopbackCandidate(true)
	settings.SetIPFilter(func(ip net.IP) bool { return ip.IsLoopback() })
	settings.SetRemoteIPFilter(func(ip net.IP) bool { return ip.IsLoopback() })
	settings.SetICEMulticastDNSMode(ice.MulticastDNSModeDisabled)
	settings.SetICEUDPMux(mux)
	settings.SetSCTPMaxMessageSize(64 << 10)
	g := gateway{
		api: webrtc.NewAPI(webrtc.WithSettingEngine(settings)), game: "http://" + loopback(*game), seats: make(chan struct{}, 32),
		client: &http.Client{Timeout: 15 * time.Second, Transport: &http.Transport{Proxy: nil}, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }},
	}
	handlers := http.NewServeMux()
	handlers.HandleFunc("/offer", g.offer)
	handlers.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) { fmt.Fprint(w, "ready") })
	server := &http.Server{Addr: loopback(*control), Handler: handlers, ReadHeaderTimeout: 3 * time.Second,
		ReadTimeout: 6 * time.Second, WriteTimeout: 8 * time.Second, IdleTimeout: 15 * time.Second, MaxHeaderBytes: 8192}
	log.Fatal(server.ListenAndServe())
}
