from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from app.db import init_db
from app.ingest import validate_and_normalize
from app.db import save_ticket, is_duplicate, update_ticket, get_all_tickets
from app.classifier import classify_ticket
from app.router import route_ticket
from app.runbook_gen import generate_runbooks
import sqlite3

app = Flask(__name__, static_folder='templates')
CORS(app)
init_db()


@app.route("/")
def dashboard():
    return send_from_directory('templates', 'dashboard.html')


@app.route("/ticket", methods=["POST"])
def ingest_ticket():
    raw = request.get_json()
    try:
        ticket = validate_and_normalize(raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if is_duplicate(ticket):
        return jsonify({"status": "duplicate"}), 200

    save_ticket(ticket)
    classification = classify_ticket(ticket)

    # Save category + confidence back to DB after classification
    confidence = classification.get("confidence", 0)
    update_ticket(ticket.id, status="classified", category=ticket.category, confidence=confidence)

    result = route_ticket(ticket, classification)

    # Update final status
    final_status = result.get("action", "escalated")
    update_ticket(ticket.id, status=final_status, category=ticket.category, confidence=confidence)

    return jsonify(result), 201


@app.route("/generate-runbooks", methods=["GET"])
def trigger_runbook_gen():
    generated = generate_runbooks()
    return jsonify({"generated": generated})


@app.route("/runbooks", methods=["GET"])
def list_runbooks():
    import os
    try:
        files = os.listdir("data/runbooks")
    except FileNotFoundError:
        files = []
    return jsonify({"runbooks": files})


@app.route("/tickets", methods=["GET"])
def list_tickets():
    tickets = get_all_tickets()
    return jsonify({"tickets": tickets})


@app.route("/stats", methods=["GET"])
def stats():
    conn = sqlite3.connect("tickets.db")
    c = conn.cursor()
    c.execute("SELECT status, COUNT(*) FROM tickets GROUP BY status")
    rows = c.fetchall()
    conn.close()
    return jsonify({row[0]: row[1] for row in rows})


if __name__ == "__main__":
    app.run(debug=True)
