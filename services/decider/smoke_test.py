"""Synthetic real-GPU probes; not task accuracy or production capacity evaluation."""
import hashlib,json,platform,statistics,subprocess,time
from pathlib import Path
from datetime import datetime
import httpx,torch,transformers
ROOT=Path(__file__).resolve().parent
TOKEN=(ROOT/'.runtime/api-token').read_text().strip()
client=httpx.Client(base_url='http://127.0.0.1:8765',headers={'Authorization':'Bearer '+TOKEN},timeout=25)
probes=[
 {'id':'en_choice','state':'The current page is Settings. The user wants to open Wi-Fi settings. Visible buttons are Wi-Fi, Bluetooth and Display.','questions':{'action':{'type':'choice','instructions':'Choose the button matching the user goal.','criteria':{'cand_wifi':'Open Wi-Fi settings','cand_bluetooth':'Open Bluetooth settings','cand_display':'Open Display settings'}}},'expected':{'action':'cand_wifi'}},
 {'id':'zh_choice','state':'当前页面是设置。用户目标是打开无线网络设置。页面中可见按钮：无线网络、蓝牙、显示。','questions':{'action':{'type':'choice','instructions':'根据用户目标，选择下一步应点击的按钮。','criteria':{'cand_wifi':'打开无线网络设置','cand_bluetooth':'打开蓝牙设置','cand_display':'打开显示设置'}}},'expected':{'action':'cand_wifi'}},
 {'id':'noul_true_false','state':'The title is Settings. Bluetooth is OFF.','questions':{'title':{'type':'noul','instructions':'Is the page title Settings?'},'bluetooth':{'type':'noul','instructions':'Is Bluetooth ON?'}},'expected':{'title':True,'bluetooth':False}}
]
rows=[]
for probe in probes:
 for repeat in range(3):
  payload={k:probe[k] for k in ('state','questions')};payload.update(independent=True,layout='state_first')
  start=time.perf_counter();r=client.post('/v1/systemone',json=payload);r.raise_for_status();body=r.json()
  for key,expected in probe['expected'].items():
   answer=body['answers'][key]
   if isinstance(expected,bool): assert (answer['noul']>0.5)==expected, (probe['id'],body)
   else: assert answer['choice']==expected, (probe['id'],body)
  rows.append({'probe':probe['id'],'repeat':repeat+1,'expected':probe['expected'],'http_status':r.status_code,'wall_ms':round((time.perf_counter()-start)*1000,2),'output':body})
boundary={}
base={'state':'Page title Home','questions':{'ready':{'type':'noul','instructions':'Is the title Home?'}}}
boundary['unauthorized']=httpx.post('http://127.0.0.1:8765/v1/systemone',json=base).status_code
boundary['long_state']=client.post('/v1/systemone',json={**base,'state':'unrelated '*1100}).status_code
boundary['body_limit']=client.post('/v1/systemone',content='x'*65537,headers={'Content-Type':'application/json'}).status_code
assert boundary=={'unauthorized':401,'long_state':422,'body_limit':413},boundary
report={'boundary_probes':boundary,'recorded_at':datetime.now().astimezone().isoformat(),'model_id':'Mapika/decider-2b','revision':'7789eb65d5cf519737608e218fa88819bddea0af','python':platform.python_version(),'torch':torch.__version__,'transformers':transformers.__version__,'cuda_runtime':torch.version.cuda,'gpu':torch.cuda.get_device_name(0),'bf16_supported':torch.cuda.is_bf16_supported(),'endpoint':'http://127.0.0.1:8765','health':client.get('/health').json(),'calls':len(rows),'rows':rows,'wall_ms_median':statistics.median(x['wall_ms'] for x in rows),'wall_ms_max':max(x['wall_ms'] for x in rows),'nvidia_smi':subprocess.check_output(['nvidia-smi','--query-gpu=name,driver_version,memory.total,memory.used','--format=csv,noheader'],text=True).strip(),'limitations':['Synthetic probes only, not Chinese task accuracy or calibration.','Not sustained capacity or p95 benchmark.','No phone actions.','Reference PyTorch kernels; no flash-linear-attention or causal_conv1d.']}
(ROOT/'.runtime/smoke-evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
print(json.dumps(report,ensure_ascii=True))
