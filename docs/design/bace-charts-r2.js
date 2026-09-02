(function(){
if (window.__baceChartsR2) return; window.__baceChartsR2 = 1;
const F="'IBM Plex Mono',monospace", FS="'IBM Plex Sans',sans-serif";
const A='#ec3013', AD='#ae1800', K='#201e1d', G='#7d7979', L='#d7d3d3', LL='#eae7e7';
function def(tag, w, h, html, attrs){
  if (customElements.get(tag)) return;
  class C extends HTMLElement{
    static get observedAttributes(){ return attrs||[]; }
    connectedCallback(){ this.style.display='block'; this._r(); }
    attributeChangedCallback(){ if(this.isConnected) this._r(); }
    _r(){
      var root = this.shadowRoot || this.attachShadow({mode:'open'});
      root.innerHTML='<svg viewBox="0 0 '+w+' '+h+'" width="100%" style="display:block;overflow:visible">'+html(this)+'</svg>';
    }
  }
  customElements.define(tag, C);
}
const t=(x,y,s,o)=>'<text x="'+x+'" y="'+y+'" style="font:'+((o&&o.f)||'400 9.5px '+F)+';fill:'+((o&&o.c)||G)+'"'+((o&&o.a)?' text-anchor="'+o.a+'"':'')+'>'+s+'</text>';
const line=(x1,y1,x2,y2,c,w,d)=>'<line x1="'+x1+'" y1="'+y1+'" x2="'+x2+'" y2="'+y2+'" stroke="'+(c||L)+'" stroke-width="'+(w||1)+'"'+(d?' stroke-dasharray="'+d+'"':'')+'/>';
const path=(d,c,w,dash)=>'<path d="'+d+'" fill="none" stroke="'+c+'" stroke-width="'+w+'"'+(dash?' stroke-dasharray="'+dash+'"':'')+' stroke-linejoin="miter"/>';
function bracket(x1,x2,y,c,label,o){
  var s=line(x1,y,x2,y,c,1)+line(x1,y-4,x1,y+4,c,1)+line(x2,y-4,x2,y+4,c,1);
  if(label) s+=t((x1+x2)/2,y-6,label,{a:'middle',f:((o&&o.bold)?'600 ':'500 ')+'9.5px '+F,c:c});
  return s;
}
function varrow(x,y1,y2,c,label){
  var s=line(x,y1,x,y2,c,1.2)+path('M'+(x-3)+' '+(y1+4)+'L'+x+' '+y1+'L'+(x+3)+' '+(y1+4),c,1.2)+path('M'+(x-3)+' '+(y2-4)+'L'+x+' '+y2+'L'+(x+3)+' '+(y2-4),c,1.2);
  if(label) s+=t(x-6,(y1+y2)/2+3,label,{a:'end',f:'600 9.5px '+F,c:c});
  return s;
}

// ---- one shot, every parameter anchored -------------------------------------------------
def('ch-shot', 900, 404, function(el){
  var axis=el.getAttribute('axis')||'vpre', pol=el.getAttribute('pol')||'INV';
  var x0=130, x1=880, xt=330, xs=444, xi=492, xl=64;
  var hv=axis==='vpre', hd=axis==='delay_ns', hc=axis==='vcoll';
  var s='';
  // lane rules
  [98,178,278].forEach(function(y){ s+=line(0,y,900,y,LL,1); });
  s+=t(8,14,'one shot · event order, not to scale',{f:'600 10px '+FS,c:K});
  s+=t(892,14,'trigger_sweep TRIG · waits for the edge, times out after acquisition_timeout_s 10',{a:'end',f:'400 9px '+FS});
  // trigger + step verticals
  s+=line(xt,24,xt,380,K,1,'3 3');
  s+=line(xs,190,xs,380,G,1,'2 3');
  s+=t(xt+4,32,'trigger',{f:'600 9.5px '+F,c:K});

  // lane 1 · Sync
  s+=t(8,52,'Sync',{f:'600 10px '+FS,c:K}); s+=t(8,64,'33220A → 81150A arm',{f:'400 9px '+FS});
  s+=path('M'+xl+' 86H'+xt+'V54H'+x1,K,1.6);
  s+=t(xt+8,60,'EXT · EDGE · POS · 1.0 V',{f:'400 9px '+FS,c:G});
  s+=t(xt+8,72,'81150A sync → CHAN3',{f:'400 9px '+FS,c:G});

  // lane 2 · LED drive
  s+=t(8,132,'LED drive',{f:'600 10px '+FS,c:K}); s+=t(8,144,'33220A out → amp → LED',{f:'400 9px '+FS});
  var ledHi=118, ledLo=166;
  if(pol==='INV'){
    s+=path('M'+xl+' '+ledHi+'H'+xt+'V'+ledLo+'H'+x1,K,1.6);
    s+=line(xl-10,ledHi,xl,ledHi,K,1.6,'2 3');
    s+=bracket(xl,xt,108,G,'pulse_width_ns 5000 · light on');
    s+=t(x1,ledLo-6,'led_low_v 0.400 V · below turn-on ≈ 1.0',{a:'end',f:'400 9px '+FS});
    s+=t(xl+6,ledHi-5,'led_v 1.020 V  ⤷ owned by illumination loop',{f:'500 9.5px '+F,c:AD});
    s+=t(xt+8,ledLo+12,':OUTP:POL INV · edge = light off',{f:'400 9px '+FS,c:G});
  } else {
    s+=path('M'+xl+' '+ledLo+'H'+xt+'V'+ledHi+'H'+x1,A,1.6);
    s+=bracket(xt,x1,108,A,'pulse_width_ns 5000 · light on — during extraction');
    s+=t(xt+8,ledLo+12,':OUTP:POL NORM · edge = light on',{f:'500 9px '+FS,c:AD});
  }
  s+=t(xl-10,ledLo+12,'period 2 ms · pulse_frequency_hz 500',{f:'400 9px '+FS});

  // lane 3 · bias
  s+=t(8,212,'Bias',{f:'600 10px '+FS,c:K}); s+=t(8,224,'81150A out → amp ×4 → device',{f:'400 9px '+FS});
  var vp=206, vc=268;
  s+=path('M'+xl+' '+vp+'H'+xs,hv?A:K,hv?2.6:1.6);
  s+=path('M'+xs+' '+vp+'V'+vc,K,1.6);
  s+=path('M'+xs+' '+vc+'H'+x1,hc?A:K,hc?2.6:1.6);
  s+=t(xl+6,vp-6,'V_pre = V_oc + 0.000 → 1.0723 V',{f:(hv?'600':'500')+' 9.5px '+F,c:hv?A:AD});
  s+=t(xl+6,vp+12,'inverted_output INV · held here between pulses',{f:'400 9px '+FS});
  s+=t(x1,vc+13,'V_coll −4.00 V · extraction',{a:'end',f:(hc?'600':'500')+' 9.5px '+F,c:hc?A:K});
  s+=bracket(xt,xs,196,hd?A:G,'delay_ns 60 · + 47 ns chain',{bold:hd});
  s+=line(xs-14,190,xs-14,202,G,1);
  if(hv) s+=varrow(xl-8,vp-14,vp+14,A,'swept · start 0.000 → stop 0.000 · zero width');
  if(hc) s+=varrow(x1+8,vc-14,vc+14,A,'');
  if(hc) s+=t(x1,vc-18,'swept',{a:'end',f:'600 9.5px '+F,c:A});
  if(hd) s+=t((xt+xs)/2,186,'swept',{a:'middle',f:'600 9.5px '+F,c:A});

  // lane 4 · device current
  s+=t(8,312,'Device current',{f:'600 10px '+FS,c:K}); s+=t(8,324,'sense 5.192 Ω → CHAN1',{f:'400 9px '+FS});
  var yb=364;
  s+='<rect x="'+xi+'" y="286" width="'+(x1-xi)+'" height="82" fill="'+A+'" fill-opacity="0.06"/>';
  s+=line(xi,286,xi,368,A,1.5);
  var d='M'+xl+' '+yb+'H'+xs+'L'+(xs+3)+' 292';
  for(var i=0;i<44;i++){ var x=xs+3+i*10, y=yb-70*Math.exp(-i/6); d+='L'+x.toFixed(1)+' '+y.toFixed(1); }
  d+='H'+x1;
  s+=path(d,K,1.5);
  s+=t(xi+6,298,'t0_int_s 1.185e-7 · from trigger',{f:'500 9.5px '+F,c:A});
  s+=t(xi+6,310,'∫ light − dark → Q · offset_correct true',{f:'400 9px '+FS,c:A});
  s+=t(xt+8,yb-6,'n_averages 100 pulses per trace',{f:'400 9px '+FS});

  // record bracket
  s+=bracket(x0,x1,390,K,'');
  s+=t(x0,402,'record · timebase_ns_per_div 500 × 10 div = 5000 ns · record_length 4000',{f:'500 9.5px '+F,c:K});
  s+=t(x1,402,'autorange on the light trace only · dark inherits the range',{a:'end',f:'400 9px '+FS});
  return s;
},['axis','pol']);

// ---- the ten-segment shot as a waveform, with a cursor ------------------------------------
def('ch-seq', 720, 128, function(el){
  var seg=parseInt(el.getAttribute('seg')||'0',10);
  var x0=64, x1=712, W=x1-x0;
  var d=[20,50,200,40,200,20,200,50,200,20], names=['set light','open shutter','settle','read power','acquire light','set dark','settle','close shutter','acquire dark','subtract ∫'];
  var xs=[x0]; d.forEach(function(v){ xs.push(xs[xs.length-1]+v/1000*W); });
  var s='';
  // done / current shading
  for(var i=0;i<10;i++){
    var c = i+1<seg ? '#201e1d' : (i+1===seg ? A : null);
    if(c) s+='<rect x="'+xs[i]+'" y="18" width="'+(xs[i+1]-xs[i])+'" height="92" fill="'+c+'" fill-opacity="'+(i+1===seg?0.10:0.04)+'"/>';
    s+=line(xs[i],18,xs[i],118,LL,1);
    s+=t((xs[i]+xs[i+1])/2,126,String(i+1),{a:'middle',f:(i+1===seg?'600':'400')+' 9px '+F,c:i+1===seg?A:G});
  }
  s+=line(x1,18,x1,118,LL,1);
  // shutter lane
  s+=t(4,30,'shutter',{f:'600 9px '+FS,c:K});
  s+=path('M'+x0+' 40H'+xs[1]+'V26H'+xs[7]+'V40H'+x1,K,1.4);
  s+=t(xs[1]+4,24,'open',{f:'400 8.5px '+FS}); s+=t(xs[8]+4,38,'shut',{f:'400 8.5px '+FS});
  // LED lane
  s+=t(4,62,'LED',{f:'600 9px '+FS,c:K});
  s+=path('M'+x0+' 72H'+xs[0]+'V58H'+xs[5]+'V72H'+x1,K,1.4);
  s+=t(xs[2]+4,56,'light levels',{f:'400 8.5px '+FS}); s+=t(xs[6]+4,70,'dark levels · shutter still open',{f:'400 8.5px '+FS});
  // acquisition lane: bursts of pulses
  s+=t(4,96,'acquire',{f:'600 9px '+FS,c:K});
  [[4,'light · autorange'],[8,'dark · range inherited']].forEach(function(b){
    var a=xs[b[0]], z=xs[b[0]+1], n=24;
    for(var k=0;k<n;k++){ var x=a+(z-a)*(k+0.5)/n; s+=line(x,104,x,90,b[0]===4?A:K,1); }
    s+=t(a+3,86,b[1],{f:'400 8.5px '+FS,c:b[0]===4?AD:G});
  });
  s+=path('M'+x0+' 104H'+x1,K,1);
  s+=t(xs[9]+3,100,'Q',{f:'600 9px '+F,c:K});
  if(seg){ s+=t(x1,12,'segment '+seg+' of 10 · '+names[seg-1],{a:'end',f:'600 9.5px '+F,c:A}); }
  s+=t(x0,12,'one loop ≈ 1 s · 10 segments · bias held at V_pre throughout, pulsed 100 × per trace',{f:'400 9px '+FS});
  return s;
},['seg']);

// ---- truncated run: 20 of 100 loops -----------------------------------------------------
def('ch-qloop-trunc', 500, 172, function(){
  var s='';
  s+='<rect x="56" y="16" width="430" height="134" fill="#fff" stroke="'+L+'"/>';
  // not acquired region
  var xcut=56+20/100*430;
  s+='<rect x="'+xcut+'" y="16" width="'+(486-xcut)+'" height="134" fill="url(#hatch)"/>';
  s='<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" stroke="'+LL+'" stroke-width="2"/></pattern></defs>'+s;
  s+=line(xcut,16,xcut,150,A,1.5);
  s+=t(xcut+6,28,'loops 21 – 100 not acquired',{f:'500 9.5px '+F,c:AD});
  s+=t(xcut+6,40,'acquisition_timeout_s 10 elapsed at loop 21 · no trigger edge',{f:'400 8.5px '+FS,c:AD});
  var q0=3.65257e-10, sig=4.01e-12, lo=3.55e-10, hi=3.75e-10;
  var Y=function(q){ return 150-(q-lo)/(hi-lo)*134; };
  [[3.75,'3.75'],[3.70,'3.70'],[3.65,'3.65'],[3.60,'3.60'],[3.55,'3.55']].forEach(function(d){ var y=Y(d[0]*1e-10); s+=line(56,y,486,y,'#f2efef',1)+t(52,y+3,d[1],{a:'end'}); });
  s+=t(56,11,'Q per loop / 1e-10 C   ·   loop →   ·   2026-08-07 archive · V_pre 0.905 V · V_coll −1 V · delay 88 ns · 290 K',{f:'600 9px '+FS,c:K});
  var r=[0.6,-1.1,0.3,1.4,-0.4,0.9,-1.6,0.2,0.7,-0.8,1.1,-0.2,0.4,-1.3,0.8,0.1,-0.6,1.0,-0.9,0.3];
  var mean='';
  var acc=0;
  for(var i=0;i<20;i++){
    var x=56+(i+0.5)/100*430, q=q0+r[i]*sig; acc+=q;
    s+='<circle cx="'+x.toFixed(1)+'" cy="'+Y(q).toFixed(1)+'" r="2" fill="'+K+'" fill-opacity="0.7"/>';
    mean+=(i?'L':'M')+x.toFixed(1)+' '+Y(acc/(i+1)).toFixed(1);
  }
  s+='<rect x="56" y="'+Y(q0+sig)+'" width="'+(xcut-56)+'" height="'+(Y(q0-sig)-Y(q0+sig))+'" fill="'+A+'" fill-opacity="0.12"/>';
  s+=path(mean,A,1.6);
  s+=t(xcut-4,Y(q0)-6,'3.65257e-10 ± 4.01e-12',{a:'end',f:'500 9.5px '+F,c:A});
  [0,20,40,60,80,100].forEach(function(v){var x=56+v/100*430; s+=line(x,150,x,154); s+=t(x,164,String(v),{a:'middle'});});
  return s;
});

// ---- ETA drift · planned vs measured per temperature step ------------------------------------
def('ch-eta', 560, 118, function(){
  var s='';
  var x0=54, W=490;
  var start=21*60+4, end0=1*60+44+24*60, end1=5*60+41+24*60; // minutes
  var span=end1-start+20;
  var X=function(m){ return x0+(m-start)/span*W; };
  var lanes=[['planned',34],['measured · predicted',74]];
  lanes.forEach(function(l){ s+=t(4,l[1]+3,l[0],{f:'600 9px '+FS,c:K}); });
  // planned segments: 120 + 8×20 measure blocks 7.5 each
  var plan=[[120,'#7d7979'],[7.5,A],[20,'#7d7979'],[7.5,A],[20,'#7d7979'],[7.5,A],[20,'#7d7979'],[7.5,A],[20,'#7d7979'],[7.5,A],[20,'#7d7979'],[7.5,A],[20,'#7d7979'],[7.5,A],[20,'#7d7979'],[7.5,A],[20,'#7d7979'],[7.5,A]];
  var m=start; plan.forEach(function(p){ s+='<rect x="'+X(m)+'" y="24" width="'+((p[0])/span*W)+'" height="20" fill="'+p[1]+'" fill-opacity="'+(p[1]===A?1:0.45)+'"/>'; m+=p[0]; });
  s+=t(X(end0)+4,38,'01:44',{f:'500 9.5px '+F,c:K});
  // measured
  var meas=[[127,'#201e1d'],[7.5,A],[22,'#201e1d'],[7.5,A],[23,'#201e1d'],[7.5,A],[181,'#201e1d'],[13,A]]; // up to 03:11
  m=start; meas.forEach(function(p){ s+='<rect x="'+X(m)+'" y="64" width="'+((p[0])/span*W)+'" height="20" fill="'+p[1]+'"/>'; m+=p[0]; });
  var now=m;
  // predicted remainder: 2 levels 3 min, then 5 × (22 + 7.5)
  var pred=[[3,A],[22,'#7d7979'],[7.5,A],[22,'#7d7979'],[7.5,A],[22,'#7d7979'],[7.5,A],[22,'#7d7979'],[7.5,A],[22,'#7d7979'],[7.5,A]];
  pred.forEach(function(p){ s+='<rect x="'+X(m)+'" y="64" width="'+((p[0])/span*W)+'" height="20" fill="none" stroke="'+(p[1]===A?A:'#7d7979')+'" stroke-dasharray="2 2"/>'; m+=p[0]; });
  s+=line(X(now),18,X(now),92,K,1.2);
  s+=t(X(now),14,'now 03:11',{a:'middle',f:'600 9.5px '+F,c:K});
  s+=t(X(m)+4,78,'05:41',{f:'600 9.5px '+F,c:A});
  // drift bracket
  s+=line(X(end0),100,X(m),100,A,1.2)+line(X(end0),96,X(end0),104,A,1)+line(X(m),96,X(m),104,A,1);
  s+=t((X(end0)+X(m))/2,112,'drift + 3 h 57 · 250 K settle took 3 h 01 against 20 min',{a:'middle',f:'500 9.5px '+F,c:AD});
  // the 250 K block label
  s+=t(X(start+127+7.5+22+7.5+23+7.5)+4,78,'250 K settle',{f:'500 9px '+F,c:'#fff'});
  return s;
});

})();
