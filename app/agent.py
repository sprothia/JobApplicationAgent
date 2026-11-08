"""Chat with the job agent in the terminal.

  python app/agent.py

Needs ANTHROPIC_API_KEY in .env.
"""

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from config.constants import ROOT
from tools import (
    add_company,
    apply_to_jobs,
    check_for_new_jobs,
    list_companies,
    list_submitted,
    remove_company,
    search_open_jobs,
)

load_dotenv(ROOT / ".env", override=True)  # .env wins over an old key set in the shell

MODEL = "anthropic:claude-sonnet-5-5"

SYSTEM_PROMPT = """You help Siddharth Prothia, a UC Santa Cruz CS student graduating March 2027, find and apply to
new grad jobs in Software Engineering, Forward Deployed Engineering, and AI Engineering. He's picky:
top companies in top locations.

Rules:
- Only report jobs your tools return. Never make up jobs, companies, or details.
- If fewer jobs match than he asked for, say how many there are. Don't pad the list.
- Keep replies short: company, title, location, and status (ready = every question answered,
  needs input = some questions left for him), plus the unanswered questions when relevant.
- Jobs come only from the companies being tracked (hand-picked Greenhouse/Ashby companies and YC startups in SF).
- search_open_jobs searches every open job he hasn't applied to or skipped, as of the last check. It's
  instant. To look for postings newer than that, call check_for_new_jobs (takes 1-2 minutes). Use it when he asks for new or latest jobs, or to "find" jobs
  and the saved ones don't cover it. Ask first if unsure.
- To apply, find the job ids with search_open_jobs, then call apply_to_jobs once with all of them. It asks him
  any missing questions itself. He always clicks Submit; never say you submitted anything.
"""

agent = create_agent(
    MODEL,
    tools=[
        tool(f)
        for f in (search_open_jobs, check_for_new_jobs, list_submitted, apply_to_jobs, list_companies, add_company, remove_company)
    ],
    system_prompt=SYSTEM_PROMPT,
    checkpointer=InMemorySaver(),  # remembers earlier messages in this chat
)


def main():
    config = {"configurable": {"thread_id": "terminal"}}
    print("Job agent. Type a message, or 'q' to quit.\n")
    while True:
        try:
            message = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if message.lower() in ("q", "quit", "exit"):
            break
        if not message:
            continue

        try:
            result = agent.invoke({"messages": [{"role": "user", "content": message}]}, config)
        except Exception as e:
            print(f"\nError: {e}\n")
            continue

        # Show which tools the agent called this turn, then its reply.
        for msg in reversed(result["messages"]):
            if msg.type == "human":
                break
        turn = result["messages"][result["messages"].index(msg) + 1 :]
        for m in turn:
            for call in getattr(m, "tool_calls", None) or []:
                print(f"  → {call['name']}({', '.join(f'{k}={v!r}' for k, v in call['args'].items())})")
        print(f"\nAgent: {result['messages'][-1].text}\n")


if __name__ == "__main__":
    main()
