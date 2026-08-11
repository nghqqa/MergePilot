# requirements.txt equivalent (parsed from comment for benchmark)
# flask==0.12.2
# requests==2.19.0
# pyyaml==3.13
# jinja2==2.10
#
# Known CVEs:
# - flask 0.12.2: CVE-2019-1010083 (DoS via crafted session cookie)
# - requests 2.19.0: CVE-2018-18074 (credential leak on redirect)
# - pyyaml 3.13: CVE-2017-18342 (arbitrary code execution via yaml.load)
# - jinja2 2.10: CVE-2019-10906 (sandbox escape)

from flask import Flask, request
import yaml
import requests as req

app = Flask(__name__)

@app.route("/config", methods=["POST"])
def update_config():
    config = yaml.load(request.data)  # unsafe yaml.load
    return {"status": "ok"}

@app.route("/proxy")
def proxy():
    url = request.args.get("url")
    resp = req.get(url)  # potential SSRF
    return resp.text
