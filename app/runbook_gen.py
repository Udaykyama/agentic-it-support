import os
import json
import uuid
from openai import OpenAI
from dotenv import load_dotenv
from app.db import get_tickets_by_category
from app.runbook_kb import add_runbook

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

CATEGORIES = ["access", "hardware", "software", "network", "other"]
MIN_CLUSTER_SIZE = 3

def generate_runbooks():
    generated = []
    for category in CATEGORIES:
        tickets = get_tickets_by_category(category, limit=20)
        if len(tickets) < MIN_CLUSTER_SIZE:
            continue

        ticket_text = "\n".join([
            f"- Title: {t[1]}\n  Description: {t[2]}"
            for t in tickets
        ])

        prompt = f"""
You are an IT operations expert. Given these {len(tickets)} support tickets 
all related to '{category}', write a structured runbook.

Tickets:
{ticket_text}

Return a JSON object with:
- title: short runbook name
- problem: one sentence describing the issue pattern
- root_cause: most likely root cause
- steps: numbered step-by-step resolution (as a single string)
- prevention: one sentence on how to prevent recurrence

Respond ONLY with valid JSON.
"""
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        runbook = json.loads(response.choices[0].message.content)
        runbook_id = str(uuid.uuid4())

        # Save to markdown file
        path = f"data/runbooks/{category}_runbook.md"
        with open(path, "w") as f:
            f.write(f"# {runbook['title']}\n\n")
            f.write(f"**Problem:** {runbook['problem']}\n\n")
            f.write(f"**Root Cause:** {runbook['root_cause']}\n\n")
            f.write(f"**Steps:**\n{runbook['steps']}\n\n")
            f.write(f"**Prevention:** {runbook['prevention']}\n")

        # Add to live ChromaDB knowledge base
        add_runbook(runbook_id, runbook["title"], runbook["steps"],
                    metadata={"category": category})
        generated.append(runbook["title"])
        print(f"Generated runbook: {runbook['title']}")

    return generated