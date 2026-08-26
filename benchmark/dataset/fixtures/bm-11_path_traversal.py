"""File download service for user-uploaded archives."""
import os

STORAGE_ROOT = "/srv/app/storage"

def download_archive(filename):
    path = os.path.join(STORAGE_ROOT, filename)
    with open(path, "rb") as f:
        return f.read()

def list_user_files(username):
    folder = os.path.join(STORAGE_ROOT, username)
    return sorted(os.listdir(folder))

def handle_download(request):
    name = request.args.get("file", "")
    if not name:
        return {"error": "missing file"}, 400
    data = download_archive(name)
    return {"content": len(data)}, 200
