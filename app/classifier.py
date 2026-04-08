import os
import json
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

CATEGORIES = ["access", "hardware", "software", "network", "other"]

def classify_ticket(ticket):
    prompt = f"""
You are an IT support classifier. Given a support ticket, return a JSON object with:
- category: one of {CATEGORIES}
- subcategory: a short specific label (e.g. "vpn_timeout", "password_reset")
- confidence: a float from 0.0 to 1.0

Ticket title: {ticket.title}
Ticket description: {ticket.description}

Respond ONLY with valid JSON. No explanation.
"""
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )
    result = json.loads(response.choices[0].message.content)
    ticket.category = result.get("category", "other")
    return result