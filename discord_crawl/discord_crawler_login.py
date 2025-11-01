BOT_TOKEN = "MTQzMDEwNzI0MDIzNjU4MDg4NA.GqgDQL.xHrCPtDWNvaUBDjFI4nWcjuefrCO3soB1glOsA"

import os
import socket
import requests
import discord
import asyncio
import aiohttp

# ======== 基本配置 ========
BOT_TOKEN = "MTQzMDEwNzI0MDIzNjU4MDg4NA.GqgDQL.xHrCPtDWNvaUBDjFI4nWcjuefrCO3soB1glOsA"  # 替换为你的 Bot Token
PROXY = "socks5h://127.0.0.1:7897"
DISCORD_API = "https://discord.com/api/v10/gateway"

# ======== 网络检测函数 ========
def test_proxy_connection(proxy=PROXY):
    print(f"🔍 正在测试本地代理 {proxy} ...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect(proxy)
        sock.close()
        print("✅ 代理端口可用")
        return True
    except Exception as e:
        print(f"❌ 无法连接到代理: {e}")
        return False

def test_discord_api():
    print("🚀 测试访问 Discord API ...")
    proxies = {
        "http": PROXY,
        "https": PROXY
    }
    try:
        r = requests.get(DISCORD_API, proxies=proxies, timeout=10)
        if r.status_code == 200:
            print(f"✅ Discord API 可访问: {r.text}")
            return True
        else:
            print(f"⚠️ Discord API 响应异常: {r.status_code}")
            return False
    except Exception as e:
        print(f"❌ 无法访问 Discord API: {e}")
        return False
    
# ======== Discord Bot 逻辑 ========
intents = discord.Intents.default()
intents.message_content = True  # 允许读取消息内容
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"🤖 Bot 已登录为: {client.user} (ID: {client.user.id})")
    print("🟢 现在可以在服务器中看到我上线了！")

@client.event
async def on_message(message):
    # 忽略自己的消息
    if message.author == client.user:
        return
    if message.content.lower() == "ping":
        await message.channel.send("pong 🏓")
    if message.content.lower() == "hi":
        await message.channel.send("你好！🤖")

# ======== 主入口 ========
async def main():
    # 为 discord.py 创建自定义 aiohttp 会话，指定代理
    async with aiohttp.ClientSession() as session:
        client.http._session = session
        client.http.proxy = PROXY  # 关键一步：设置代理
        await client.start(BOT_TOKEN)

if __name__ == "__main__":
    if test_discord_api():
        asyncio.run(main())
    else:
        print("❌ 网络检测失败，请检查代理设置。")
