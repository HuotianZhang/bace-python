(function(){
if (window.__baceChartsR3) return; window.__baceChartsR3 = 1;
const F="'IBM Plex Mono',monospace", FS="'IBM Plex Sans',sans-serif";
const A='#ec3013', AD='#ae1800', K='#201e1d', G='#7d7979', L='#d7d3d3', LL='#eae7e7', W='#fff';
function def(tag, w, h, html, attrs){
  if (customElements.get(tag)) return;
  class C extends HTMLElement{
    static get observedAttributes(){ return attrs||[]; }
    connectedCallback(){ this.style.display='block'; this._r(); }
    attributeChangedCallback(){ if(this.isConnected) this._r(); }
    _r(){ var root=this.shadowRoot||this.attachShadow({mode:'open'}); root.innerHTML='<svg viewBox="0 0 '+w+' '+h+'" width="100%" style="display:block;overflow:visible">'+html(this)+'</svg>'; }
  }
  customElements.define(tag, C);
}
const t=(x,y,s,o)=>'<text x="'+x+'" y="'+y+'" style="font:'+((o&&o.f)||'400 9.5px '+F)+';fill:'+((o&&o.c)||G)+'"'+((o&&o.a)?' text-anchor="'+o.a+'"':'')+((o&&o.tr)?' transform="'+o.tr+'"':'')+'>'+s+'</text>';
const line=(x1,y1,x2,y2,c,w,d)=>'<line x1="'+x1+'" y1="'+y1+'" x2="'+x2+'" y2="'+y2+'" stroke="'+(c||L)+'" stroke-width="'+(w||1)+'"'+(d?' stroke-dasharray="'+d+'"':'')+'/>';
const path=(d,c,w,dash,o)=>'<path d="'+d+'" fill="none" stroke="'+c+'" stroke-width="'+w+'"'+(dash?' stroke-dasharray="'+dash+'"':'')+((o&&o.cap)?' stroke-linecap="'+o.cap+'"':'')+((o&&o.m)?' marker-end="url(#'+o.m+')"':'')+((o&&o.op)?' stroke-opacity="'+o.op+'"':'')+' stroke-linejoin="miter"/>';
const rect=(x,y,w,h,f,o)=>'<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="'+f+'"'+(o&&o.op!=null?' fill-opacity="'+o.op+'"':'')+(o&&o.s?' stroke="'+o.s+'" stroke-width="'+(o.sw||1)+'"'+(o.d?' stroke-dasharray="'+o.d+'"':''):'')+'/>';
function bracket(x1,x2,y,c,label,bold){
  var s=line(x1,y,x2,y,c,1)+line(x1,y-4,x1,y+4,c,1)+line(x2,y-4,x2,y+4,c,1);
  if(label) s+=t((x1+x2)/2,y-6,label,{a:'middle',f:(bold?'600 ':'500 ')+'9.5px '+F,c:c});
  return s;
}
const markers='<defs><marker id="ak" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" fill="'+K+'"/></marker><marker id="aa" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" fill="'+A+'"/></marker></defs>';

// ---- dark J-V · log|J| · diode model (no dark curve in the data pack) ----------------------
def('ch-jvdark', 500, 330, function(){
  var x0=56,x1=490,y0=16,y1=300;
  var X=function(v){return x0+(v+0.2)/1.4*(x1-x0)}, Y=function(l){return y1-(l+6)/8*(y1-y0)};
  var s=rect(x0,y0,x1-x0,y1-y0,W,{s:L});
  [-6,-4,-2,0,2].forEach(function(l){ s+=line(x0,Y(l),x1,Y(l),'#f2efef',1)+t(x0-4,Y(l)+3,l===0?'1':'1e'+l,{a:'end'}); });
  s+=line(X(0),y0,X(0),y1,LL,1);
  var J0=2e-8,n=1.6,Vt=0.02543,Rsh=1e7,Rs=6,d='';
  for(var i=0;i<=140;i++){
    var v=-0.2+i*0.01, lo=-1, hi=1e4;
    for(var k=0;k<70;k++){ var J=(lo+hi)/2, Vd=v-J*1e-3*Rs, f=J0*(Math.exp(Vd/(n*Vt))-1)+Vd*1e3/Rsh-J; if(f>0) lo=J; else hi=J; }
    var l=Math.log10(Math.max(Math.abs((lo+hi)/2),1e-6));
    d+=(i?'L':'M')+X(v).toFixed(1)+' '+Y(Math.min(l,2)).toFixed(1);
  }
  s+=path(d,K,1.6);
  [-0.2,0,0.4,0.8,1.2].forEach(function(v){ s+=line(X(v),y1,X(v),y1+4)+t(X(v),y1+14,String(v).replace('-','−'),{a:'middle'}); });
  s+=t(x0,10,'|J| / mA cm⁻²   ·   V / V →',{f:'600 9.5px '+FS,c:K});
  s+=t(x1,10,'151 points · 0.05 s each',{a:'end',f:'400 9px '+FS});
  return s;
});

// ---- power monitor · sparkline -----------------------------------------------------------
def('ch-spark', 500, 64, function(){
  var x0=56,x1=470,y0=8,y1=52, vals=[], seed=7;
  for(var i=0;i<60;i++){ seed=(seed*9301+49297)%233280; vals.push(1.407+0.005*(seed/233280-0.5)+0.002*Math.sin(i/6)); }
  var mn=1.398,mx=1.416, Y=function(v){return y1-(v-mn)/(mx-mn)*(y1-y0)};
  var s=rect(x0,y0,x1-x0,y1-y0,W,{s:L}), d='';
  vals.forEach(function(v,i){ d+=(i?'L':'M')+(x0+i/59*(x1-x0)).toFixed(1)+' '+Y(v).toFixed(1); });
  s+=path(d,K,1.4);
  s+='<circle cx="'+x1+'" cy="'+Y(vals[59]).toFixed(1)+'" r="3" fill="'+A+'"/>';
  s+=t(x0-4,Y(mx)+3,'1.416',{a:'end'})+t(x0-4,Y(mn)+3,'1.398',{a:'end'});
  s+=t(x1+6,Y(vals[59])+3,'mW',{c:AD,f:'500 9.5px '+F});
  s+=t(x0,y1+10,'−60 s',{f:'400 9px '+F})+t(x1,y1+10,'now',{a:'end',f:'400 9px '+F});
  return s;
});

// ---- Q per loop · 20 repeats, cursor at 12 ------------------------------------------------
def('ch-qloop20', 600, 130, function(){
  var x0=56,x1=586,y0=14,y1=106, lo=8.0e-10,hi=9.0e-10;
  var Y=function(q){return y1-(q-lo)/(hi-lo)*(y1-y0)}, X=function(i){return x0+(i-0.5)/20*(x1-x0)};
  var s=rect(x0,y0,x1-x0,y1-y0,W,{s:L});
  [8.0,8.5,9.0].forEach(function(v){ var y=Y(v*1e-10); s+=line(x0,y,x1,y,'#f2efef',1)+t(x0-4,y+3,v.toFixed(1),{a:'end'}); });
  var dev=[-0.6,1.1,-0.3,0.9,-1.4,0.4,0.2,-0.8,1.6,-0.5,0.3,-0.9], sig=1.83e-11, base=8.52e-10, acc=0, mean='';
  s+=rect(x0,Y(base+sig),X(12)-x0,Y(base-sig)-Y(base+sig),A,{op:.12});
  dev.forEach(function(r,i){ var q=base+r*sig; acc+=q; s+='<circle cx="'+X(i+1).toFixed(1)+'" cy="'+Y(q).toFixed(1)+'" r="2.4" fill="'+K+'" fill-opacity=".7"/>'; mean+=(i?'L':'M')+X(i+1).toFixed(1)+' '+Y(acc/(i+1)).toFixed(1); });
  s+=path(mean,A,1.6);
  s+=line(X(12),y0,X(12),y1,K,1,'2 2');
  [0,5,10,15,20].forEach(function(v){ var x=x0+v/20*(x1-x0); s+=line(x,y1,x,y1+4)+t(x,y1+14,String(v),{a:'middle'}); });
  s+=t(x0,9,'Q / 1e-10 C   ·   loop →',{f:'600 9.5px '+FS,c:K});
  s+=t(x1,9,'zero-width axis · repeats, not a curve',{a:'end',f:'400 9px '+FS});
  return s;
});

// ---- the chain ------------------------------------------------------------------------------
function box(x,y,w,h,lines,o){
  var s=rect(x,y,w,h,(o&&o.f)||W,{s:(o&&o.s)||K,sw:(o&&o.sw)||1,d:o&&o.d});
  s+=t(x+9,y+17,lines[0],{f:'600 11.5px '+FS,c:(o&&o.c)||K});
  if(lines[1]) s+=t(x+9,y+31,lines[1],{f:'400 9.5px '+FS,c:G});
  if(lines[2]) s+=t(x+9,y+45,lines[2],{f:'400 9.5px '+F,c:(o&&o.c2)||G});
  return s;
}
def('ch-rig', 960, 470, function(){
  var s=markers;
  // instruments
  s+=box(44,30,220,58,['33220A','LED drive · master clock','1 kHz · 50 % · low 0.400 V']);
  s+=box(44,118,220,58,['81150A','bias pulser','ARM EXT POS · :PULS:DEL1 = delay_ns'],{s:A,sw:1.4});
  s+=box(44,206,220,58,['Keithley 2400','SMU · DC','V_oc · J_sc · J_sat · cc ≤ 50 mA']);
  s+=box(44,294,220,58,['Infiniium','digitiser','CHAN1 I(t) · CHAN3 trigger · avg 100']);
  s+=box(44,382,220,58,['Deditec DIO','shutter = module 0 · relay = module 1','ID 9'],{s:G,d:'3 3'});
  // bench
  s+=box(344,30,90,58,['LED amp','fixed gain','≈ 83 ns']);
  s+=box(464,30,70,58,['LED','530 nm','']);
  s+=box(564,30,120,58,['shutter → fibre','DIO 0 · open / shut','85 m fibre · 419 ns']);
  s+=box(714,30,70,58,['splitter','meter +','device']);
  s+=box(814,30,130,58,['1918-C','optical power','W · :8918']);
  s+=box(344,118,90,58,['× 4 amp','bias',''],{s:A,sw:1.4});
  s+=box(514,190,150,84,['relay · DIO 1','2400  ↔  amplifier · never both',''],{sw:1.4});
  s+=box(734,190,210,70,['device in cryostat','s4 · PTQ10:IT-4F · pixel a','V_pre held · pulsed to V_coll'],{sw:2});
  s+=box(734,300,210,50,['R_sense 5.192 Ω','I = V / R',''],{});
  s+=box(734,390,210,50,['Lake Shore 331','cryostat temperature · not wired','console :8331'],{s:G,d:'3 3'});
  // relay poles
  s+='<circle cx="530" cy="246" r="3" fill="'+K+'"/><circle cx="530" cy="264" r="3" fill="'+K+'"/><circle cx="648" cy="255" r="3" fill="'+K+'"/>';
  s+=line(530,264,648,255,K,1.6);
  s+=t(538,243,'amp',{f:'400 8.5px '+FS})+t(538,270,'2400',{f:'400 8.5px '+FS});
  // drive
  s+=path('M264 59H342',K,1.6,null,{m:'ak'}); s+=path('M434 59H462',K,1.6,null,{m:'ak'});
  s+=t(268,54,'drive',{f:'400 9px '+FS});
  // light
  s+=path('M534 59H562',K,2,'1 4',{cap:'round',m:'ak'}); s+=path('M684 59H712',K,2,'1 4',{cap:'round',m:'ak'}); s+=path('M784 59H812',K,2,'1 4',{cap:'round',m:'ak'});
  s+=path('M749 88V188',K,2,'1 4',{cap:'round',m:'ak'});
  s+=t(755,140,'light · 502 ns',{f:'400 9px '+FS});
  // trigger 33220A SYNC -> 81150A ARM
  s+=path('M264 78H290V128H266',A,1.6,'6 3',{m:'aa'});
  s+=t(294,106,'SYNC ↑ arms',{f:'500 9px '+FS,c:AD});
  // 81150A SYNC -> scope CHAN3 (left gutter)
  s+=path('M44 165H22V340H42',A,1.6,'6 3',{m:'aa'});
  s+=t(14,252,'SYNC → CHAN3',{f:'500 9px '+FS,c:AD,a:'middle',tr:'rotate(-90 14 252)'});
  // bias
  s+=path('M264 147H342',A,2,null,{m:'aa'}); s+=path('M434 147H474V246H512',A,2,null,{m:'aa'});
  s+=t(438,142,'V_pre → V_coll',{f:'500 9px '+FS,c:AD});
  // DC
  s+=path('M264 264H512',K,1.6,null,{m:'ak'}); s+=t(300,260,'DC',{f:'400 9px '+FS});
  // relay -> device
  s+=path('M664 255H732',K,1.8,null,{m:'ak'});
  // device -> R_sense -> scope
  s+=path('M839 260V298',K,1.6,null,{m:'ak'});
  s+=path('M734 325H266',K,1.6,null,{m:'ak'});
  s+=t(600,320,'I(t) → CHAN1',{f:'400 9px '+FS});
  s+=t(44,462,'host: setup and read-back only · the 1 kHz loop closes in hardware',{f:'400 9.5px '+FS});
  return s;
});

// ---- one shot at four scales · chain timing measured 2026-09-01 (zero = 81150A Sync) ----------
def('ch-timing', 1000, 760, function(el){
  var axis=el.getAttribute('axis')||'vpre', hv=axis==='vpre', hd=axis==='delay_ns', hc=axis==='vcoll';
  var s='';
  // A · one shot
  var XA=function(sec){return 90+sec/0.8*890};
  s+=t(8,18,'A · one shot = one Q · ≈ 0.8 s',{f:'600 11px '+FS,c:K});
  s+=t(980,18,'one loop = one shot per axis point · zero-width axis: loop = shot · n_loops repeats',{a:'end',f:'400 10px '+FS});
  s+=rect(90,26,890,134,W,{s:L});
  s+=t(8,52,'shutter',{f:'600 10px '+FS,c:K}); s+=t(8,80,'LED drive',{f:'600 10px '+FS,c:K}); s+=t(8,91,'1 kHz · 50 %',{f:'400 8.5px '+FS}); s+=t(8,112,'bias',{f:'600 10px '+FS,c:K}); s+=t(8,123,'pulsed each cycle',{f:'400 8.5px '+FS}); s+=t(8,148,'acquire',{f:'600 10px '+FS,c:K});
  s+=path('M90 56H'+XA(0.03)+'V40H'+XA(0.43)+'V56H980',K,1.6);
  s+=t(XA(0.06),37,'open',{f:'400 9px '+FS}); s+=t(XA(0.46),66,'shut',{f:'400 9px '+FS});
  var per=890/80, dled='M90 74';
  for(var i=0;i<80;i++){ var xa=90+i*per; dled+='H'+(xa+per/2).toFixed(1)+'V92H'+(xa+per).toFixed(1)+'V74'; }
  s+=path(dled,K,1);
  
  var xs=XA(0.43), yhL=104, ybL=122, yhD=108, ybD=126;
  s+=path('M90 '+yhL+'H'+xs,hv?A:K,1.4); s+=path('M'+xs+' '+yhD+'H980',K,1.4);
  for(var j=0;j<80;j++){ var xb=90+j*per+per/2, lt=xb<xs; s+=line(xb,lt?yhL:yhD,xb,lt?ybL:ybD,hc?A:K,1); }
  s+=t(90,170,'LED drive never stops · the light at the sample follows 502 ns later   ·   bias: V_pre ⇄ V_coll (1.0423 → −4.00 V) for 5 µs at every LED-off edge   ·   dark half: 0 ⇄ −5.04 V, the same swing shifted by −V_oc',{f:'400 9px '+FS});
  s+=rect(XA(0.25),136,XA(0.35)-XA(0.25),18,A); s+=rect(XA(0.65),136,XA(0.75)-XA(0.65),18,K);
  s+=t(XA(0.30),148,'light · 100 cycles',{a:'middle',f:'500 9px '+FS,c:W}); s+=t(XA(0.70),148,'dark · 100 cycles',{a:'middle',f:'500 9px '+FS,c:W});
  s+=t(XA(0.14),148,'settle_s 0.20',{a:'middle'}); s+=t(XA(0.54),148,'dark_settle_s 0.20',{a:'middle'}); s+=t(XA(0.775),148,'Q',{a:'middle',f:'600 10px '+F,c:K});
  s+=rect(XA(0.295)-2,72,5,22,'none',{s:A,d:'2 2'});
  s+=path('M'+(XA(0.295)-2)+' 94L90 200',A,1,'3 3',{op:.6}); s+=path('M'+(XA(0.295)+3)+' 94L980 200',A,1,'3 3',{op:.6});

  // B · one cycle
  var XB=function(ms){return 90+ms*890};
  s+=t(8,194,'B · one LED cycle · 1 ms',{f:'600 11px '+FS,c:K});
  s+=t(980,194,'one of n_averages 100 · summed in the scope',{a:'end',f:'400 10px '+FS});
  s+=rect(90,200,890,156,W,{s:L});
  s+=t(8,224,'LED drive',{f:'600 10px '+FS,c:K}); s+=t(8,253,'light at sample',{f:'600 10px '+FS,c:K}); s+=t(8,279,'SYNC',{f:'600 10px '+FS,c:K}); s+=t(8,310,'bias',{f:'600 10px '+FS,c:K}); s+=t(8,342,'scope',{f:'600 10px '+FS,c:K});
  s+=path('M90 212H'+XB(0.5)+'V230H980',K,1.6);
  s+=t(XB(0.05),208,'on · led_v 1.020 V · 500 µs',{f:'400 9px '+FS}); s+=t(XB(0.75),226,'off · led_low_v 0.400 · 500 µs',{f:'400 9px '+FS});
  s+=path('M90 242H'+XB(0.5)+'V256H980',K,1.6,'5 3');
  s+=t(XB(0.05),239,'same square, 502 ns later · LED amp 83 ns + 85 m fibre 419 ns',{f:'400 9px '+FS});
  s+=path('M90 282H'+XB(0.5)+'V268H980',A,1.6);
  s+=t(XB(0.52),265,'SYNC ↑ · 380 ns after the drive-off edge · arms 81150A · scope trigger',{f:'500 9px '+FS,c:AD});
  var xp=XB(0.5)+0.5;
  s+=path('M90 296H'+xp,hv?A:K,hv?2.4:1.6); s+=path('M'+xp+' 296V318H'+(xp+5)+'V296',hc?A:K,hc?2.4:1.6); s+=path('M'+(xp+5)+' 296H980',hv?A:K,hv?2.4:1.6);
  s+=t(XB(0.05),291,'V_pre = V_oc + 0.000 → 1.0423 V',{f:(hv?'600':'500')+' 9.5px '+F,c:hv?A:K});
  s+=t(xp+20,307,':PULS:DEL1 = delay_ns 60 · V_coll −4.00 V for 5 µs',{f:((hd||hc)?'600':'400')+' 9px '+F,c:hd?A:(hc?A:G)});
  s+=t(xp+20,319,'the 5 µs record starts 0.48 µs before the Sync',{f:'400 9px '+F,c:G});
  s+=line(xp+1,330,xp+1,346,A,1.6);
  s+=t(xp+20,341,'trigger ← 81150A SYNC · one record per cycle',{f:'400 9px '+FS});
  s+=rect(xp-8,288,22,60,'none',{s:A,d:'2 2'});
  s+=path('M'+(xp-8)+' 348L90 390',A,1,'3 3',{op:.6}); s+=path('M'+(xp+14)+' 348L980 390',A,1,'3 3',{op:.6});

  // C · the edges · zero = 81150A Sync
  var XC=function(ns){return 90+(ns+480)/780*890};
  s+=t(8,384,'C · the edges · −480 … +300 ns · zero = 81150A Sync = scope trigger · measured 2026-09-01',{f:'600 11px '+FS,c:K});
  s+=rect(90,390,890,170,W,{s:L});
  s+=t(8,409,'LED drive',{f:'600 10px '+FS,c:K}); s+=t(8,433,'SYNC · CH3',{f:'600 10px '+FS,c:K}); s+=t(8,463,'bias',{f:'600 10px '+FS,c:K}); s+=t(8,493,'light at sample',{f:'600 10px '+FS,c:K}); s+=t(8,530,'photocurrent',{f:'600 10px '+FS,c:K});
  [-380,0,60,122].forEach(function(v){ s+=line(XC(v),392,XC(v),558,LL,1); });
  s+=path('M90 402H'+XC(-380)+'V416H980',K,1.6);
  s+=t(XC(-380)+5,413,'−380 · drive off',{f:'500 9px '+F,c:K});
  s+=path('M90 442H'+XC(0)+'V428H980',A,1.6);
  s+=t(XC(0)-5,439,'0 · Sync = trigger',{a:'end',f:'500 9px '+F,c:AD});
  s+=path('M90 450H'+XC(60),hv?A:K,hv?2.4:1.6); s+=path('M'+XC(60)+' 450V472H980',hc?A:K,hc?2.4:1.6);
  s+=t(XC(-460),447,'V_pre 1.0423 V',{f:(hv?'600':'500')+' 9px '+F,c:hv?A:K});
  s+=t(XC(60)+8,458,'+60 · :PULS:DEL1 = delay_ns · V_coll −4.00 V',{f:((hd||hc)?'600':'500')+' 9px '+F,c:hd?A:(hc?A:K)});
  s+=path('M90 484H'+XC(122)+'V498H980',K,1.6,'5 3');
  s+=t(XC(107)-8,495,'light off at the sample · +122 · 502 ns after the drive edge',{a:'end',f:'500 9px '+F,c:K});
  s+=rect(XC(107),506,XC(122)-XC(107),42,A,{op:.14});
  var dc='M90 540H'+XC(107)+'L'+XC(122)+' 512';
  for(var k=1;k<=12;k++){ var tn=122+k*15, y=512+28*(1-Math.exp(-(tn-122)/700)); dc+='L'+XC(tn).toFixed(1)+' '+y.toFixed(1); }
  s+=path(dc,K,1.6);
  s+=t(XC(107)-8,522,'response +107 … 122 · t_bias 47–62 ns after the output edge',{a:'end',f:'400 9px '+F,c:G});
  s+=line(XC(118.5),506,XC(118.5),548,A,1.5);
  s+=t(XC(122)+8,546,'t0_int +118.5',{f:'600 9px '+F,c:A});
  [-400,-300,-200,-100,0,100,200].forEach(function(v){ s+=line(XC(v),560,XC(v),564,K,1)+t(XC(v),574,String(v).replace('-','−'),{a:'middle'}); });
  s+=t(980,574,'300 ns',{a:'end'});

  // D · the record
  var XD=function(ns){return 90+(ns+480)/5000*890};
  s+=t(8,594,'D · the record · 5 µs · timebase 500 ns/div · 3125 pts · dt 1.60 ns',{f:'600 11px '+FS,c:K});
  s+=rect(90,600,890,130,W,{s:L});
  s+=t(8,626,'bias',{f:'600 10px '+FS,c:K}); s+=t(8,680,'photocurrent',{f:'600 10px '+FS,c:K});
  s+=path('M90 614H'+XD(60),hv?A:K,hv?2.4:1.6); s+=path('M'+XD(60)+' 614V634H980',hc?A:K,hc?2.4:1.6);
  s+=t(XD(-460),610,'V_pre',{f:'500 9.5px '+F,c:hv?A:K});
  s+=t(976,610,'V_coll −4.00 V · back to V_pre at +5.06 µs, after the record ends',{a:'end',f:(hc?'600':'500')+' 9.5px '+F,c:hc?A:K});
  s+=rect(XD(118.5),642,980-XD(118.5),80,A,{op:.07});
  s+=line(XD(118.5),642,XD(118.5),722,A,1.5);
  var dd='M90 712H'+XD(107)+'L'+XD(122)+' 660';
  for(var m=1;m<=60;m++){ var tm=122+m*73, ym=712-52*Math.exp(-(tm-122)/700); dd+='L'+XD(tm).toFixed(1)+' '+ym.toFixed(1); }
  dd+='H980';
  s+=path(dd,K,1.6);
  s+=t(XD(118.5)+6,652,'t0_int 118.5 ns from trigger',{f:'600 9.5px '+F,c:A});
  s+=t(980-6,656,'Q = ∫ (light − dark) dt',{a:'end',f:'500 9.5px '+F,c:A});
  [0,1000,2000,3000,4000].forEach(function(v){ s+=line(XD(v),730,XD(v),734,K,1)+t(XD(v),744,String(v),{a:'middle'}); });
  s+=t(980,744,'ns from trigger',{a:'end',f:'400 9px '+FS});
  s+=rect(90,602,XD(300)-90,126,'none',{s:A,d:'2 2'});
  s+=path('M90 602L90 560',A,1,'3 3',{op:.6}); s+=path('M'+XD(300)+' 602L980 560',A,1,'3 3',{op:.6});
  return s;
},['axis']);

// ---- pipeline schedule · measured settle gaps 2026-08-31 ------------------------------------
def('ch-schedule', 1200, 130, function(){
  var x0=70,x1=1180,y0=34,y1=66, meas=7.3;
  var steps=[[295,0],[290,113],[280,7],[270,22],[260,10],[250,37],[240,2],[230,11],[220,4]];
  var total=steps.reduce(function(a,st){return a+st[1]+meas},0);
  var X=function(m){return x0+m/total*(x1-x0)};
  var s='', m=0;
  steps.forEach(function(st){
    if(st[1]>0){ s+=rect(X(m),y0,X(m+st[1])-X(m),y1-y0,'#7d7979',{op:.32}); if(st[1]>=10) s+=t((X(m)+X(m+st[1]))/2,y0+20,st[1]+' min',{a:'middle',f:'400 9.5px '+F,c:'#605d5d'}); }
    m+=st[1];
    s+=rect(X(m),y0,X(m+meas)-X(m),y1-y0,A);
    s+=t((X(m)+X(m+meas))/2,y0-8,st[0]+' K',{a:'middle',f:'600 10.5px '+F,c:K});
    m+=meas;
  });
  [0,60,120,180,240].forEach(function(mm){ var x=X(mm), h=(21+Math.floor(mm/60))%24; s+=line(x,y1,x,y1+5,'#bab6b6',1)+t(x,y1+16,(h<10?'0':'')+h+':04',{a:'middle'}); });
  s+=line(x1,y1,x1,y1+5,K,1); s+=t(x1,y1+16,'01:36',{a:'end',f:'600 9.5px '+F,c:K});
  s+=t(x0,y1+34,'start 21:04',{f:'400 9.5px '+F,c:G}); s+=t(x1,y1+34,'finish 01:36 · 4 h 32',{a:'end',f:'600 9.5px '+F,c:K});
  s+=t(8,y0+20,'T',{f:'600 10px '+FS,c:K});
  return s;
});

})();
