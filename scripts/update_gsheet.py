# -*- coding: utf-8 -*-
import sys, io, json, base64, time, datetime
from urllib.parse import quote
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import requests, rsa, openpyxl
from pyasn1.codec.der import decoder as der_decoder
from pyasn1_modules import rfc5208

SA=r'C:\Users\1\.config\mcp-google-sheets\service-account.json'
SID='YOUR_GOOGLE_SHEET_ID'
sa=json.load(open(SA, encoding='utf-8'))
def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b'=')
pem=sa['private_key']; body=''.join(l for l in pem.splitlines() if 'PRIVATE KEY' not in l)
pki,_=der_decoder.decode(base64.b64decode(body), asn1Spec=rfc5208.PrivateKeyInfo())
priv=rsa.PrivateKey.load_pkcs1(bytes(pki['privateKey']), format='DER')
now=int(time.time())
hdr=b64u(json.dumps({"alg":"RS256","typ":"JWT"}).encode())
claim=b64u(json.dumps({"iss":sa['client_email'],
  "scope":"https://www.googleapis.com/auth/spreadsheets","aud":sa['token_uri'],
  "iat":now,"exp":now+3600}).encode())
si=hdr+b'.'+claim
assertion=(si+b'.'+b64u(rsa.sign(si,priv,'SHA-256'))).decode()
tok=requests.post(sa['token_uri'], data={"grant_type":"urn:ietf:params:oauth:grant-type:jwt-bearer","assertion":assertion}, timeout=30).json()['access_token']
H={"Authorization":f"Bearer {tok}"}
print("token OK")

wb=openpyxl.load_workbook(r'C:\Users\1\camera\Все_камеры.xlsx', data_only=True)
meta=requests.get(f"https://sheets.googleapis.com/v4/spreadsheets/{SID}", headers=H, timeout=30).json()
existing={s['properties']['title']: s['properties']['sheetId'] for s in meta['sheets']}
print("existing tabs:", list(existing))

def coerce(v):
    if v is None: return ""
    if isinstance(v,(str,int,float,bool)): return v
    if isinstance(v,(datetime.datetime,datetime.date)): return str(v)
    return str(v)

# create missing tabs
reqs=[{"addSheet":{"properties":{"title":ws.title}}} for ws in wb.worksheets if ws.title not in existing]
if reqs:
    r=requests.post(f"https://sheets.googleapis.com/v4/spreadsheets/{SID}:batchUpdate", headers=H, json={"requests":reqs}, timeout=60)
    r.raise_for_status(); print("added tabs:", [q['addSheet']['properties']['title'] for q in reqs])

for ws in wb.worksheets:
    rows=[[coerce(c) for c in row] for row in ws.iter_rows(values_only=True)]
    while rows and all(c=='' for c in rows[-1]): rows.pop()
    # clear then write
    requests.post(f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{quote(ws.title)}:clear", headers=H, timeout=60)
    u=f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{quote(ws.title+'!A1')}?valueInputOption=RAW"
    rr=requests.put(u, headers=H, json={"values":rows}, timeout=180); rr.raise_for_status()
    print(f"  '{ws.title}': {len(rows)} rows -> {rr.json().get('updatedCells')} cells")

# format headers of all tabs
meta=requests.get(f"https://sheets.googleapis.com/v4/spreadsheets/{SID}", headers=H, timeout=30).json()
reqs=[]
for s in meta['sheets']:
    sid=s['properties']['sheetId']
    reqs.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":0,"endRowIndex":1},
      "cell":{"userEnteredFormat":{"backgroundColor":{"red":0.12,"green":0.18,"blue":0.33},
        "textFormat":{"bold":True,"foregroundColor":{"red":1,"green":1,"blue":1}}}},
      "fields":"userEnteredFormat(textFormat,backgroundColor)"}})
    reqs.append({"updateSheetProperties":{"properties":{"sheetId":sid,"gridProperties":{"frozenRowCount":1}},
      "fields":"gridProperties.frozenRowCount"}})
requests.post(f"https://sheets.googleapis.com/v4/spreadsheets/{SID}:batchUpdate", headers=H, json={"requests":reqs}, timeout=60)
print("headers formatted")
print("URL: https://docs.google.com/spreadsheets/d/"+SID+"/edit")
