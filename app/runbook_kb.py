import chromadb
from chromadb.utils import embedding_functions

client = chromadb.PersistentClient(path="./chroma_db")
ef = embedding_functions.DefaultEmbeddingFunction()
collection = client.get_or_create_collection("runbooks", embedding_function=ef)

def add_runbook(runbook_id, title, steps, metadata=None):
    collection.upsert(
        ids=[runbook_id],
        documents=[f"{title}\n{steps}"],
        metadatas=[{"title": title, "steps": steps, **(metadata or {})}]
    )

def search_runbook(subcategory, description, n_results=1):
    query = f"{subcategory} {description}"
    results = collection.query(query_texts=[query], n_results=n_results)
    if results["ids"][0]:
        meta = results["metadatas"][0][0]
        return {"title": meta["title"], "steps": meta["steps"]}
    return None