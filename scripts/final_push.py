# -*- coding: utf-8 -*-
"""Финальный пуш в Google Sheets: все листы + колонка Q '=IMAGE()' + высота строк."""
import sys, io, json, base64, time, datetime
from urllib.parse import quote
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
import requests, rsa, openpyxl
from pyasn1.codec.der import decoder as der_decoder
from pyasn1_modules import rfc5208

SC=r'C:\Users\1\AppData\Local\Temp\claude\C--Users-1-camera\f4622068-9a25-4606-977d-aa386243eb4e\scratchpad'
SID='YOUR_GOOGLE_SHEET_ID'
sa=json.load(open(r'C:\Users\1\.config\mcp-google-sheets\service-account.json', encoding='utf-8'))
def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b'=')
pb=''.join(l for l in sa['private_key'].splitlines() if 'PRIVATE KEY' not in l)
pki,_=der_decoder.decode(base64.b64decode(pb), asn1Spec=rfc5208.PrivateKeyInfo())
priv=rsa.PrivateKey.load_pkcs1(bytes(pki['privateKey']), format='DER')
now=int(time.time())
si=b64u(json.dumps({"alg":"RS256","typ":"JWT"}).encode())+b'.'+b64u(json.dumps({
  "iss":sa['client_email'],"scope":"https://www.googleapis.com/auth/spreadsheets","aud":sa['token_uri'],
  "iat":now,"exp":now+3600}).encode())
tok=requests.post(sa['token_uri'],data={"grant_type":"urn:ietf:params:oauth:grant-type:jwt-bearer",
  "assertion":(si+b'.'+b64u(rsa.sign(si,priv,'SHA-256'))).decode()},timeout=30).json()['access_token']
H={"Authorization":f"Bearer {tok}"}
print("token OK")

su=json.load(open(SC+r'\snap_urls.json'))
row2id={int(k):v for k,v in su['rows'].items() if v}
print("snapshot urls:", len(row2id))

wb=openpyxl.load_workbook(r'C:\Users\1\camera\Все_камеры.xlsx', data_only=True)
def coerce(v):
    if v is None: return ""
    if isinstance(v,(str,int,float,bool)): return v
    if isinstance(v,(datetime.datetime,datetime.date)): return str(v)
    return str(v)

meta=requests.get(f"https://sheets.googleapis.com/v4/spreadsheets/{SID}", headers=H, timeout=30).json()
tabs={s['properties']['title']: s['properties']['sheetId'] for s in meta['sheets']}
adds=[{"addSheet":{"properties":{"title":w.title}}} for w in wb.worksheets if w.title not in tabs]
if adds:
    requests.post(f"https://sheets.googleapis.com/v4/spreadsheets/{SID}:batchUpdate", headers=H, json={"requests":adds}, timeout=60).raise_for_status()
    meta=requests.get(f"https://sheets.googleapis.com/v4/spreadsheets/{SID}", headers=H, timeout=30).json()
    tabs={s['properties']['title']: s['properties']['sheetId'] for s in meta['sheets']}

for w in wb.worksheets:
    rows=[[coerce(c) for c in row] for row in w.iter_rows(values_only=True)]
    while rows and all(c=='' for c in rows[-1]): rows.pop()
    if w.title=='Все камеры':
        for i,row in enumerate(rows):
            r=i+1
            while len(row)<17: row.append("")
            if r==1: row[16]='Снимок'
            elif r in row2id: row[16]=f'=IMAGE("https://drive.google.com/uc?export=view&id={row2id[r]}")'
            else: row[16]=''
    requests.post(f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{quote(w.title)}:clear", headers=H, timeout=60)
    rr=requests.put(f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{quote(w.title+'!A1')}?valueInputOption=USER_ENTERED",
        headers=H, json={"values":rows}, timeout=300)
    rr.raise_for_status()
    print(f"  '{w.title}': {len(rows)} rows -> {rr.json().get('updatedCells')} cells")

# formatting: headers bold+navy, freeze, row heights on Все камеры
reqs=[]
for t,sid in tabs.items():
    reqs.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":0,"endRowIndex":1},
      "cell":{"userEnteredFormat":{"backgroundColor":{"red":0.12,"green":0.18,"blue":0.33},
        "textFormat":{"bold":True,"foregroundColor":{"red":1,"green":1,"blue":1}}}},
      "fields":"userEnteredFormat(textFormat,backgroundColor)"}})
    reqs.append({"updateSheetProperties":{"properties":{"sheetId":sid,"gridProperties":{"frozenRowCount":1}},
      "fields":"gridProperties.frozenRowCount"}})
cam_sid=tabs['Все камеры']
nrows=wb['Все камеры'].max_row
reqs.append({"updateDimensionProperties":{"range":{"sheetId":cam_sid,"dimension":"ROWS","startIndex":1,"endIndex":nrows},
  "properties":{"pixelSize":72},"fields":"pixelSize"}})
reqs.append({"updateDimensionProperties":{"range":{"sheetId":cam_sid,"dimension":"COLUMNS","startIndex":16,"endIndex":17},
  "properties":{"pixelSize":132},"fields":"pixelSize"}})
requests.post(f"https://sheets.googleapis.com/v4/spreadsheets/{SID}:batchUpdate", headers=H, json={"requests":reqs}, timeout=120).raise_for_status()
print("formatted (headers, freeze, row heights 72px, col Q 132px)")
print("URL: https://docs.google.com/spreadsheets/d/"+SID+"/edit")
