package main

// 第一性原理：行情网关是 I/O 密集型，Go 协程 + context 超时最优
// 当前离线 Demo 不发起真实调用，本文件为后续同花顺/JoinQuant 并发拉取预留
import (
    "context"
    "fmt"
    "time"
)

type Snapshot struct {
    Symbol string  `json:"symbol"`
    Price  float64 `json:"price"`
    TS     string  `json:"timestamp"`
}

func fetchSnapshot(ctx context.Context, symbol string) (Snapshot, error) {
    select {
    case <-ctx.Done():
        return Snapshot{}, ctx.Err()
    case <-time.After(50 * time.Millisecond): // 模拟网络
        return Snapshot{Symbol: symbol, Price: 1.045, TS: time.Now().UTC().Format(time.RFC3339)}, nil
    }
}

func main() {
    ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
    defer cancel()
    snap, err := fetchSnapshot(ctx, "159502.SZ")
    if err != nil { fmt.Println("gateway error:", err); return }
    fmt.Printf("gateway snapshot %s %.4f %s (placeholder, not live)\n", snap.Symbol, snap.Price, snap.TS)
}
