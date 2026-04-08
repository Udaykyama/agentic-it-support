from flask import Flask, request, jsonify
from app.db import init_db
from app.ingest import validate_and_normalize
from app.db import save_ticket, is_duplicate, update_ticket
from app.classifier import classify_ticket
from app.router import route_ticket
from app.runbook_gen import generate_runbooks

app = Flask(__name__)
init_db()

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
    
    # Save category back to database after classification
    update_ticket(ticket.id, status="classified", category=ticket.category)
    
    result = route_ticket(ticket, classification)
    
    # Update final status
    final_status = result.get("action", "escalated")
    update_ticket(ticket.id, status=final_status, category=ticket.category)
    
    return jsonify(result), 201

@app.route("/generate-runbooks", methods=["GET"])
def trigger_runbook_gen():
    generated = generate_runbooks()
    return jsonify({"generated": generated})

@app.route("/runbooks", methods=["GET"])
def list_runbooks():
    import os
    files = os.listdir("data/runbooks")
    return jsonify({"runbooks": files})

if __name__ == "__main__":
    app.run(debug=True)