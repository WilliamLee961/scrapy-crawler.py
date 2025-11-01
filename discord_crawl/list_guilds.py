import discord
import asyncio
from aiohttp_socks import ProxyConnector  # pip install aiohttp_socks
# 服务器id：1430028817593794645
TOKEN = "MTQzMDEwNzI0MDIzNjU4MDg4NA.GqgDQL.xHrCPtDWNvaUBDjFI4nWcjuefrCO3soB1glOsA"
PROXY_URL = "http://127.0.0.1:7897"

intents = discord.Intents.default()
intents.guilds = True

async def main():
    connector = ProxyConnector.from_url(PROXY_URL)  # 这里必须在事件循环运行时调用

    # 把 connector 直接传给 discord.py
    async with discord.Client(intents=intents, connector=connector) as client:
        
        @client.event
        async def on_ready():
            print(f"✅ 已登录：{client.user}")
            for guild in client.guilds:
                print(f"- {guild.name} (ID: {guild.id})")
            await client.close()

        await client.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())