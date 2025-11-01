BOT_TOKEN = "MTQzMDEwNzI0MDIzNjU4MDg4NA.GqgDQL.xHrCPtDWNvaUBDjFI4nWcjuefrCO3soB1glOsA"  # 不要泄露！
"""
Discord 爬虫（使用 discord.py）
功能：
- 列出 bot 所在的 guilds（服务器）
- 为指定 guild_id 遍历 text channels，抓取消息历史、附件并保存为 JSON 文件
- 抓取频道元数据、成员与角色信息

使用：
export DISCORD_BOT_TOKEN="你的 token"
python discord_crawler_doubao.py --guild 1430107240236580884  --out ./data --proxy http://127.0.0.1:7897"
"""
import os
import json
import asyncio
import argparse
from pathlib import Path
import aiohttp
import discord
from discord.errors import HTTPException, Forbidden, NotFound
from datetime import datetime
from aiohttp_socks import ProxyConnector
import contextlib

def now_str():
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

async def save_json_atomic(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)

# ---------- 核心爬虫 ----------
class DiscordCrawler(discord.Client):
    def __init__(self, *, out_dir: Path, proxy: str | None = None, **kwargs):
        # 如果有代理，给 discord.Client 传入 connector，让它所有连接都走代理
        if proxy:
            connector = ProxyConnector.from_url(proxy)
        else:
            connector = None

        super().__init__(connector=connector, **kwargs)
        self.out_dir = out_dir
        self.proxy = proxy
        self.conn_for_download = connector  # 保存下载用的 connector
        self.session: aiohttp.ClientSession | None = None
    
    async def on_ready(self):
        print(f"Logged in as {self.user} (id={self.user.id})")
        # 用相同的 connector 创建附件下载的 session
        self.session = aiohttp.ClientSession(connector=self.conn_for_download)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        await super().close()
    
    async def crawl_guild(self, guild_id: int, max_messages_per_channel: int | None = None):
        guild = self.get_guild(guild_id)
        if guild is None:
            # 尝试 fetch
            try:
                guild = await self.fetch_guild(guild_id)
            except Exception as e:
                print(f"无法找到 guild {guild_id}: {e}")
                return
        out_guild = self.out_dir / f"{guild.id}_{guild.name}"
        out_guild.mkdir(parents=True, exist_ok=True)

        meta = {
            "id": guild.id,
            "name": guild.name,
            "member_count": guild.member_count,
            "roles": [],
            "channels": []
        }

        for r in guild.roles:
            meta["roles"].append({
                "id": r.id, "name": r.name, "position": r.position, "permissions": r.permissions.value
            })
        
        try: # 尝试获取成员名（需要intent允许）
            members = []
            async for m in guild.fetch_members(limit=None):
                members.append({
                    "id": m.id, "name": str(m), "display_name": m.display_name, "bot": m.bot
                })
            meta["members_count_fetched"] = len(members)
            await save_json_atomic(out_guild / "members.json", members)
        except Exception as e:
            print(f"警告：无法完整抓取成员（可能没有启用 Server Members Intent）：{e}")
        
        for ch in guild.text_channels:
            ch_meta = {"id": ch.id, "name": ch.name, "topic": ch.topic, "nsfw": ch.nsfw}
            print(f"> 采集频道: #{ch.name} ({ch.id})")
            meta["channels"].append(ch_meta)

            messages_out = []
            attachments_dir = out_guild / "attachments" / f"{ch.id}_{ch.name}"
            attachments_dir.mkdir(parents=True, exist_ok=True)

            # 使用通用的消息历史迭代器（async for）
            count = 0
            try:
                async for msg in ch.history(limit=max_messages_per_channel):  # limit=None 表示全部（注意耗时）
                    count += 1
                    m = {
                        "id": msg.id,
                        "created_at": msg.created_at.isoformat(),
                        "author": {"id": msg.author.id, "name": str(msg.author)},
                        "content": msg.content,
                        "edited_at": msg.edited_at.isoformat() if msg.edited_at else None,
                        "pinned": msg.pinned,
                        "attachments": [],
                        "embeds": [e.to_dict() for e in msg.embeds] if msg.embeds else []
                    }

                    # attachments: 下载到本地并记录文件名/路径
                    for att in msg.attachments:
                        fname = f"{att.id}_{att.filename}"
                        fpath = attachments_dir / fname
                        # 下载
                        try:
                            await self._download_attachment(att.url, fpath)
                            m["attachments"].append({"filename": att.filename, "saved_path": str(fpath)})
                        except Exception as e:
                            print(f"下载附件失败 {att.url}: {e}")
                            m["attachments"].append({"filename": att.filename, "url": att.url, "error": str(e)})
                    messages_out.append(m)
                print(f"  共采集消息 {count} 条")
            except (Forbidden, HTTPException) as e:
                print(f"  ! 无法读取频道 #{ch.name}: {e}")
            # 保存消息为 json（每个频道一个文件）
            await save_json_atomic(out_guild / f"channel_{ch.id}_{ch.name}_messages.json", messages_out)
        # 保存 guild 元数据
        await save_json_atomic(out_guild / "guild_meta.json", meta)
        print(f"完成 guild {guild.id} 的采集，保存路径: {out_guild}")

    async def _download_attachment(self, url: str, dest_path: Path, retry=3):
        # 使用我们自己的 session 下载（可通过 proxy）
        if self.session is None:
            raise RuntimeError("http session 未初始化")
        for attempt in range(1, retry+1):
            try:
                # 如果需要代理，可在这里传 proxy=self.proxy
                async with self.session.get(url, proxy=self.proxy, timeout=30) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        dest_path.write_bytes(data)
                        return
                    else:
                        raise RuntimeError(f"HTTP {resp.status}")
            except Exception as e:
                if attempt < retry:
                    await asyncio.sleep(1 * attempt)
                else:
                    raise

# ---------- 运行入口 ----------
def build_client(intents, out_dir: Path, proxy: str | None):
    return DiscordCrawler(intents=intents, out_dir=out_dir, proxy=proxy)

async def run_crawler(args):
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("请先通过环境变量 DISCORD_BOT_TOKEN 提供 bot token（不要硬编码）")

    intents = discord.Intents.default()
    intents.message_content = True    # 读取消息内容需要在开发者门户启用 Message Content Intent
    intents.guilds = True
    intents.members = True

    client = build_client(intents=intents, out_dir=Path(args.out), proxy=args.proxy)
    # 如果你想使用与现有脚本同样的方式设置 client.http._session / proxy，可在这里做（不推荐）
    try:
        await client.login(token)
        # on_ready 会被调用，之后手动调用 crawl 方法
        await client.connect()
    except KeyboardInterrupt:
        print("收到中断，退出")
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--guild", type=int, required=False, help="要爬取的 guild ID（不填则只登录不爬取）")
    parser.add_argument("--out", type=str, default="./discord_data", help="输出目录")
    parser.add_argument("--proxy", type=str, default=None, help="可选代理 e.g. http://127.0.0.1:7897")
    parser.add_argument("--max-per-channel", type=int, default=1000, help="每个频道最多抓取的消息数（默认1000，None 为全部）")
    args = parser.parse_args()        

    # 运行：我们先登录，然后在交互中调用 crawl_guild（为简单，这里用更直接的模式）
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("请先设置环境变量 DISCORD_BOT_TOKEN，并在开发者门户开启必要 intents。")
        raise SystemExit(1)
    
    # 为了便于使用（避免 bot 立刻退出），我们直接在脚本中以更可控方式启动并调用 crawl（简化版）
    async def main():
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True

        client = DiscordCrawler(intents=intents, out_dir=Path(args.out), proxy=args.proxy)
        try:
            await client.login(token)
            await client._async_setup_hook()  # 内部 setup，确保 ready 能触发
            # 使用 start 以触发 on_ready，随后我们可以调用 crawl
            task = asyncio.create_task(client.start(token))
            # 等待 client.ready
            await client.wait_until_ready()
            print("client ready -> 开始爬取 (如果提供 guild id)")
            if args.guild:
                try:
                    await client.crawl_guild(
                        args.guild, 
                        max_messages_per_channel=(None if args.max_per_channel<=0 else args.max_per_channel)
                        )
                except Exception as e:
                    print("采集过程中出错:", e)
        except KeyboardInterrupt:
            print("收到中断，退出")
        
        finally:
            # 确保任务取消并等待它退出
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            # 这里一定要等 close 完成
            await client.close()

if __name__ == "__main__":
    asyncio.run(main())
