import os
import socket
import requests
from urllib.parse import urlparse
# discord bot token:MTQzMDEwNzI0MDIzNjU4MDg4NA.GqgDQL.xHrCPtDWNvaUBDjFI4nWcjuefrCO3soB1glOsA
DISCORD_API = "https://discord.com/api/v10/gateway"
os.environ['HTTPS_PROXY'] = "http://127.0.0.1:7897"
os.environ['HTTP_PROXY'] = "http://127.0.0.1:7897"

def check_proxy_env():
    """检测系统代理环境变量"""
    proxies = {
        "http": os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"),
        "https": os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    }
    print("🌐 系统代理配置：")
    for k, v in proxies.items():
        print(f"  {k.upper()} = {v if v else '（未设置）'}")
    return proxies


def test_proxy_connectivity(proxy_url: str):
    """测试代理端口是否可达"""
    if not proxy_url:
        return False, "未配置代理"
    try:
        parsed = urlparse(proxy_url)
        host, port = parsed.hostname, parsed.port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect((host, port))
        sock.close()
        return True, f"✅ 代理 {host}:{port} 可访问"
    except Exception as e:
        return False, f"❌ 无法连接代理 {proxy_url} ({e})"


def test_discord_access(proxies=None):
    """尝试访问 Discord API"""
    try:
        print("\n🚀 测试访问 Discord API ...")
        resp = requests.get(DISCORD_API, proxies=proxies, timeout=5)
        if resp.status_code == 200:
            print("✅ Discord API 可访问！")
            return True
        else:
            print(f"⚠️ Discord API 响应异常: {resp.status_code}")
            return False
    except requests.exceptions.ProxyError:
        print("❌ 代理配置无效或被拒绝连接")
        return False
    except requests.exceptions.ConnectTimeout:
        print("❌ 连接 Discord 超时（可能被墙）")
        return False
    except Exception as e:
        print(f"❌ 访问 Discord 失败: {e}")
        return False


def main():
    print("=== Discord 网络连通性测试 ===\n")
    proxies = check_proxy_env()

    # 检查代理端口可用性
    for key, proxy in proxies.items():
        ok, msg = test_proxy_connectivity(proxy)
        print(f"  [{key.upper()}] {msg}")

    # 访问 Discord API
    test_discord_access(proxies if proxies["https"] else None)

    print("\n=== 测试完成 ===")
    print("💡 如果显示超时或被拒绝，请开启 VPN/Clash/V2Ray 并设置系统代理，例如：")
    print('   setx HTTPS_PROXY "http://127.0.0.1:7890"')
    print('   setx HTTP_PROXY "http://127.0.0.1:7890"')
    print("   然后重新运行此脚本。")

if __name__ == "__main__":
    main()