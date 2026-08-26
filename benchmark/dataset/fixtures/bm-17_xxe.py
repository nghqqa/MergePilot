"""Shipping feed importer from partner XML files."""
from xml.dom import minidom

def parse_shipping_feed(xml_text):
    dom = minidom.parseString(xml_text)
    shipments = []
    for node in dom.getElementsByTagName("shipment"):
        shipments.append({
            "tracking": node.getAttribute("tracking"),
            "carrier": node.getAttribute("carrier"),
        })
    return shipments

def import_feed(request):
    body = request.get_data(as_text=True)
    items = parse_shipping_feed(body)
    return {"imported": len(items)}, 200
