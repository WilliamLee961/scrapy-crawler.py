import aiohttp, asyncio, os

os.environ["ALL_PROXY"] = "socks5://127.0.0.1:7897"

async def test_gateway():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect("wss://gateway.discord.gg/?v=10&encoding=json", timeout=10) as ws:
                print("✅ 成功连接到 Discord Gateway WebSocket！")
                msg = await ws.receive(timeout=5)
                print("👂 收到服务端握手数据：", msg.data[:120], "...")
    except Exception as e:
        print("❌ 连接失败：", e)

asyncio.run(test_gateway())