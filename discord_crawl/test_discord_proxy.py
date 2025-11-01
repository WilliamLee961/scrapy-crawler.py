import socket
import ssl

PROXY_HOST = "127.0.0.1"   # 代理IP
PROXY_PORT = 7897          # 代理端口 (HTTP 或 SOCKS5 端口)

DISCORD_HOST = "discord.com"
DISCORD_PORT = 443

def test_proxy_connection():
    print(f"🛠 测试通过代理 {PROXY_HOST}:{PROXY_PORT} 连接 Discord...")

    try:
        # 建立 TCP 连接到代理
        sock = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=10)
        print("✅ 已连接到代理")

        # 发送 HTTP CONNECT 请求（适用于 HTTP 代理）
        connect_req = f"CONNECT {DISCORD_HOST}:{DISCORD_PORT} HTTP/1.1\r\nHost: {DISCORD_HOST}\r\n\r\n"
        sock.sendall(connect_req.encode())

        # 接收代理返回
        response = sock.recv(4096).decode(errors="ignore")
        print("代理返回:", response.strip())

        if "200" not in response:
            print("❌ 代理拒绝 CONNECT 到 Discord")
            return False

        # 建立 SSL
        context = ssl.create_default_context()
        ssl_sock = context.wrap_socket(sock, server_hostname=DISCORD_HOST)
        print("✅ SSL 握手成功，可以访问 Discord")

        # 发送一个简单的 HTTPS 请求
        ssl_sock.sendall(f"GET /api/v10/gateway HTTP/1.1\r\nHost: discord.com\r\n\r\n".encode())
        data = ssl_sock.recv(4096)
        print("Discord API 返回:", data.decode(errors="ignore")[:200], "...")

        ssl_sock.close()
        return True

    except Exception as e:
        print("❌ 连接失败:", e)
        return False


if __name__ == "__main__":
    if test_proxy_connection():
        print("🎉 代理可以访问 Discord，请尝试运行你的主程序")
    else:
        print("⚠ 代理无法连接 Discord，请检查代理配置或网络规则")
