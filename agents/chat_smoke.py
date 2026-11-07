"""Gate 1c: two local uAgents exchange an Agent Chat Protocol message (Bureau)."""
from datetime import datetime, timezone
from uuid import uuid4

from uagents import Agent, Bureau, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec,
)

alice = Agent(name="smoke_worker", seed="antibody-smoke-worker-seed-2026")
bob = Agent(name="smoke_registry", seed="antibody-smoke-registry-seed-2026")


def make_proto(reply: bool):
    proto = Protocol(spec=chat_protocol_spec)

    @proto.on_message(ChatMessage)
    async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
        text = "".join(c.text for c in msg.content if isinstance(c, TextContent))
        ctx.logger.info(f"CHAT from {sender[:16]}…: {text}")
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc),
                                                   acknowledged_msg_id=msg.msg_id))
        if reply:
            await ctx.send(sender, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                               content=[TextContent(type="text", text=f"registry got: {text}")]))
        else:
            ctx.logger.info("SMOKE_OK")

    @proto.on_message(ChatAcknowledgement)
    async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
        ctx.logger.info(f"ACK for {msg.acknowledged_msg_id}")

    return proto


alice.include(make_proto(reply=False))
bob.include(make_proto(reply=True))


@alice.on_event("startup")
async def kickoff(ctx: Context):
    await ctx.send(bob.address, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                            content=[TextContent(type="text", text="lookup sig=TypeError:float")]))


if __name__ == "__main__":
    bureau = Bureau(port=8099)
    bureau.add(alice)
    bureau.add(bob)
    bureau.run()
