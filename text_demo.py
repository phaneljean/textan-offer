"""
text_demo.py -- the homepage "Text an offer" demo (2026-10-24, Phanel-approved
mockup: ad-creative/2026-10-24_home_text_phone/mock_interactive.html).

Three steps play in a loop: the terms are typed and sent, the confirmation
comes back, YES is sent, the draft-ready reply comes back, then page 1 of the
draft slides in and its values fill one by one. Nothing here is invented:

- The SMS text is the real copy from format_offer_confirmation() and the
  draft-ready reply in app.py, captured from a local run of the SMS flow for
  "725k 3% 21day 123 Main St, Austin, TX" (shortened: the free-offers count,
  profile tip and STOP/HELP lines are left off the bubbles).
- static/text_demo/v_*.png are crops of the filled fields from the PDF that
  run generated (page 1: 2A city/address, 3A/3B/3C, Third Party Financing
  box); page_thumb.jpg is the top of that page.

If the SMS copy or the 20-19 field mapping changes, re-capture these.
Classes are td- prefixed: the homepage already uses .paper/.row/.tag/.bub.
Starts when scrolled into view; pause/replay buttons; prefers-reduced-motion
shows the finished state without animation.
"""

CSS = """
  /* ---------- 3. text an offer: interactive demo (text_demo.py) ---------- */
  .text{padding-bottom:110px}
  .td{display:grid;grid-template-columns:340px 1fr;gap:56px;max-width:1040px;margin:64px auto 0;align-items:center;text-align:left}
  .td-tabs{display:flex;flex-direction:column;gap:10px}
  .td-tab{all:unset;box-sizing:border-box;cursor:pointer;display:block;border-radius:18px;padding:18px 20px 20px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);transition:background .25s,border-color .25s}
  .td-tab:hover{background:rgba(255,255,255,.07)}
  .td-tab:focus-visible{outline:2px solid #9fd4c9;outline-offset:3px}
  .td-t{display:flex;align-items:center;gap:12px;font-size:18px;font-weight:700;color:#cfe3df}
  .td-n{width:28px;height:28px;border-radius:50%;border:1.5px solid rgba(255,255,255,.35);display:grid;place-items:center;font-size:13px;flex:none}
  .td-tab p{font-size:15px;line-height:1.45;color:#8fb1aa;margin:8px 0 0 40px;max-height:0;overflow:hidden;opacity:0;transition:max-height .35s,opacity .35s}
  .td-bar{height:3px;border-radius:3px;background:rgba(255,255,255,.12);margin:14px 0 0 40px;overflow:hidden;display:none}
  .td-bar i{display:block;height:100%;width:0;background:#9fd4c9}
  .td-tab.on{background:rgba(255,255,255,.09);border-color:rgba(159,212,201,.45)}
  .td-tab.on .td-t{color:#fff}
  .td-tab.on .td-n{background:#fff;color:var(--green-deep);border-color:#fff}
  .td-tab.on p{max-height:90px;opacity:1}
  .td-tab.on .td-bar{display:block}
  .td-tab.done .td-n{background:#9fd4c9;border-color:#9fd4c9;color:var(--green-deep)}
  .td-ctl{display:flex;gap:14px;margin:8px 0 0 4px}
  .td-ctl button{all:unset;cursor:pointer;font-size:13px;font-weight:600;color:#9fd4c9;padding:6px 2px}
  .td-ctl button:focus-visible{outline:2px solid #9fd4c9;outline-offset:2px;border-radius:4px}
  .td-stage{position:relative;height:620px}
  .td-phone{position:absolute;left:0;top:0;width:300px;background:#0d0d0d;border-radius:46px;padding:11px;box-shadow:0 30px 60px -20px rgba(0,0,0,.55);transition:transform .6s cubic-bezier(.2,.8,.2,1),opacity .4s}
  .td-screen{background:#fff;border-radius:36px;height:598px;overflow:hidden;position:relative;color:var(--ink);text-align:left}
  .td-island{position:absolute;top:10px;left:50%;transform:translateX(-50%);width:96px;height:27px;background:#0d0d0d;border-radius:20px;z-index:3}
  .td-who{position:absolute;top:0;left:0;right:0;padding:50px 0 12px;text-align:center;font-size:11px;color:var(--mute);border-bottom:1px solid #eee;background:#fff;z-index:2}
  .td-av{width:38px;height:38px;border-radius:50%;background:var(--green);margin:0 auto 5px;display:grid;place-items:center;color:#fff;font-weight:800;font-size:17px;font-family:Georgia,serif}
  .td-who b{display:block;color:var(--ink);font-size:13px;font-weight:600}
  .td-thread{position:absolute;left:0;right:0;top:122px;bottom:58px;padding:0 12px 10px;display:flex;flex-direction:column;justify-content:flex-end;gap:8px;overflow:hidden}
  .td-bub{max-width:86%;padding:9px 13px;border-radius:19px;font-size:13.5px;line-height:1.36;white-space:pre-line;flex:none;animation:td-pop .32s cubic-bezier(.2,.9,.3,1.2) both}
  .td-me{align-self:flex-end;background:var(--green);color:#fff;border-bottom-right-radius:6px}
  .td-them{align-self:flex-start;background:#e9e9eb;border-bottom-left-radius:6px}
  .td-them u{color:var(--green);text-decoration:none;font-weight:600}
  .td-time{align-self:center;font-size:10.5px;color:#8e8e93;flex:none}
  .td-dots{align-self:flex-start;background:#e9e9eb;border-radius:19px;padding:12px 14px;display:flex;gap:4px;flex:none;animation:td-pop .25s both}
  .td-dots i{width:7px;height:7px;border-radius:50%;background:#9a9aa0;animation:td-blink 1s infinite}
  .td-dots i:nth-child(2){animation-delay:.15s}.td-dots i:nth-child(3){animation-delay:.3s}
  @keyframes td-blink{0%,60%,100%{opacity:.3}30%{opacity:1}}
  @keyframes td-pop{from{opacity:0;transform:translateY(8px) scale(.96)}to{opacity:1;transform:none}}
  .td-composer{position:absolute;left:10px;right:10px;bottom:12px;display:flex;gap:8px;align-items:center}
  .td-field{flex:1;min-height:34px;border:1px solid #d9d9de;border-radius:18px;padding:8px 13px;font-size:13.5px;color:var(--ink);line-height:1.3;white-space:nowrap;overflow:hidden}
  .td-field:empty:before{content:"Text Message";color:#b0b0b5}
  .td-field.typing:after{content:"";display:inline-block;width:1.5px;height:14px;background:var(--green);margin-left:1px;vertical-align:-2px;animation:td-blink .9s infinite}
  .td-send{width:30px;height:30px;border-radius:50%;background:#d9d9de;display:grid;place-items:center;flex:none;transition:background .2s}
  .td-send.ready{background:var(--green)}
  .td-send svg{width:14px;height:14px}
  .td-paper{position:absolute;right:0;top:34px;width:400px;background:#fff;border-radius:16px;padding:18px 18px 20px;color:var(--ink);text-align:left;
            box-shadow:0 40px 80px -24px rgba(0,0,0,.6);opacity:0;transform:translateX(40px) scale(.97);pointer-events:none;transition:opacity .5s,transform .6s cubic-bezier(.2,.8,.2,1)}
  .td-stage.pdf .td-paper{opacity:1;transform:none}
  .td-stage.pdf .td-phone{transform:translateX(-20px) scale(.94);transform-origin:left center}
  .td-ph{display:flex;justify-content:space-between;font-size:11px;font-weight:700;letter-spacing:.06em;color:var(--mute);text-transform:uppercase;padding-bottom:12px;border-bottom:1px solid var(--line)}
  .td-ph span:last-child{color:var(--green)}
  .td-thumb{height:104px;overflow:hidden;margin-top:12px;border:1px solid var(--line);border-radius:6px;position:relative}
  .td-thumb img{width:100%;display:block}
  .td-thumb:after{content:"";position:absolute;left:0;right:0;bottom:0;height:40px;background:linear-gradient(transparent,#fff)}
  .td-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px solid #f0f0f2}
  .td-row span{font-size:13px;font-weight:600;color:var(--mute);white-space:nowrap}
  .td-row img{height:19px;width:auto;max-width:60%;object-fit:contain;object-position:left;border-radius:2px;outline:2px solid transparent;outline-offset:3px;opacity:0;transition:opacity .35s,outline-color .35s}
  .td-row.fill img{opacity:1;outline-color:var(--green)}
  .td-row.fill span{color:var(--ink)}
  .td-note{display:inline-block;margin-top:12px;font-size:12px;font-weight:700;color:var(--green);opacity:0;transition:opacity .4s}
  .td-files{display:flex;flex-wrap:wrap;gap:6px;margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}
  .td-files span{font-size:12px;font-weight:600;padding:5px 9px;border-radius:7px;background:#f2f6f5;color:var(--green-deep);opacity:0;transform:translateY(4px);transition:opacity .3s,transform .3s}
  .td-paper.all .td-note,.td-paper.all .td-files span{opacity:1;transform:none}
  .td-fine{font-size:13px;color:#8fb1aa;max-width:620px;margin:40px auto 0;line-height:1.5}
  @media (max-width:980px){
    .td{grid-template-columns:1fr;gap:28px;max-width:420px}
    .td-tabs{flex-direction:row;gap:6px}
    .td-tab{flex:1;padding:12px 10px}
    .td-t{font-size:13px;gap:7px;flex-direction:column;text-align:center}
    .td-tab p{display:none}
    .td-bar{margin:10px 0 0}
    .td-ctl{justify-content:center}
    .td-stage{height:620px;display:flex;justify-content:center}
    .td-phone{position:relative;left:auto}
    .td-paper{left:50%;right:auto;top:40px;width:min(360px,100%);transform:translate(-50%,40px) scale(.97)}
    .td-stage.pdf .td-paper{transform:translate(-50%,0)}
    .td-stage.pdf .td-phone{transform:scale(.92);opacity:.35}
  }
  @media (max-width:734px){ .text{padding-bottom:72px} }
  @media (prefers-reduced-motion:reduce){ .td *{animation:none!important;transition:none!important} }
"""

HTML = """
  <div class="td" id="textDemo">
    <div>
      <div class="td-tabs" role="tablist" aria-label="How texting an offer works">
        <button class="td-tab" type="button" role="tab" data-step="0"><span class="td-t"><span class="td-n">1</span>Text the terms</span><p>Price, down payment, closing days and the address, in one text.</p><span class="td-bar"><i></i></span></button>
        <button class="td-tab" type="button" role="tab" data-step="1"><span class="td-t"><span class="td-n">2</span>Reply YES</span><p>Check the summary it sends back, then confirm.</p><span class="td-bar"><i></i></span></button>
        <button class="td-tab" type="button" role="tab" data-step="2"><span class="td-t"><span class="td-n">3</span>Open the filled PDF</span><p>The TREC 20&#8209;19 and its addenda, filled from your text.</p><span class="td-bar"><i></i></span></button>
      </div>
      <div class="td-ctl"><button id="tdPause" type="button">Pause</button><button id="tdReplay" type="button">Replay</button></div>
    </div>
    <div class="td-stage" id="tdStage" aria-live="polite">
      <div class="td-phone"><div class="td-screen"><div class="td-island"></div>
        <div class="td-who"><div class="td-av">t</div><b>TxtAnOffer</b>(833) 897-0333</div>
        <div class="td-thread" id="tdThread"></div>
        <div class="td-composer"><div class="td-field" id="tdField"></div>
          <div class="td-send" id="tdSend"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg></div></div>
      </div></div>
      <div class="td-paper" id="tdPaper">
        <div class="td-ph"><span>TREC 20-19 &middot; Page 1 of 12</span><span>Draft</span></div>
        <div class="td-thumb"><img src="/static/text_demo/page_thumb.jpg" alt="Page 1 of the generated TREC 20-19 draft for 123 Main St" loading="lazy"></div>
        <div class="td-row"><span>2A &middot; City of</span><img src="/static/text_demo/v_city.png" alt="Austin" loading="lazy"></div>
        <div class="td-row"><span>2A &middot; Known as</span><img src="/static/text_demo/v_address.png" alt="123 Main St" loading="lazy"></div>
        <div class="td-row"><span>3A &middot; Cash portion</span><img src="/static/text_demo/v_cash.png" alt="$21,750" loading="lazy"></div>
        <div class="td-row"><span>3B &middot; Financing</span><img src="/static/text_demo/v_fin.png" alt="$703,250" loading="lazy"></div>
        <div class="td-row"><span>3C &middot; Sales Price</span><img src="/static/text_demo/v_price.png" alt="$725,000" loading="lazy"></div>
        <div class="td-row"><span>3B &middot; Third Party Financing Addendum</span><img src="/static/text_demo/v_tpfa.png" alt="Checked" loading="lazy"></div>
        <span class="td-note">Filled in from your text</span>
        <div class="td-files"><span>TREC 20-19</span><span>40-11 Financing Addendum</span><span>61-0 Water Disclosure</span><span>IABS</span></div>
      </div>
    </div>
  </div>
  <p class="td-fine">Shown: the real replies and the real draft for the 123 Main St sample. Names and other terms are left blank for you to complete. Draft only: review every page before anyone signs.</p>
"""

JS = r"""
<script>
(function(){
  var root = document.getElementById("textDemo"); if(!root) return;
  var OFFER = "725k 3% 21day 123 Main St, Austin, TX";
  var REPLY1 = "TxtAnOffer: got it.\n\n123 Main St, Austin, TX\nPrice $725,000, 3% down ($21,750)\nLoan $703,250\nEarnest $7,250, option fee $250\nClosing Nov 14 (21 days)\n\nReply YES to create the TREC draft, or text a correction (ex: \"make it 820k\").";
  var REPLY2 = "Your TREC draft for 123 Main St is ready.\n\nReview and email it: <u>txtanoffer.com/review/&hellip;</u>\n\nIncludes TREC 20-19, 40-11 Financing Addendum, 61-0 Water Disclosure and IABS. Draft only: review every page before signing.";
  var DUR = [9000, 6500, 7500];
  var $ = function(id){ return document.getElementById(id); };
  var thread = $("tdThread"), field = $("tdField"), send = $("tdSend"), stage = $("tdStage"), paper = $("tdPaper");
  var tabs = [].slice.call(root.querySelectorAll(".td-tab")), rows = [].slice.call(paper.querySelectorAll(".td-row"));
  var reduced = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var run = 0, paused = false;
  function wait(ms, id){ return new Promise(function(res, rej){ var left = ms, last = Date.now();
    (function tick(){ if(id !== run) return rej("stop"); var now = Date.now(); if(!paused && !document.hidden) left -= now - last; last = now;
      if(left <= 0) return res(); setTimeout(tick, 40); })(); }); }
  function add(cls, html){ var d = document.createElement("div"); d.className = cls; d.innerHTML = html; thread.appendChild(d); return d; }
  function esc(s){ return s.replace(/&/g,"&amp;").replace(/</g,"&lt;"); }
  function setTabs(i){ tabs.forEach(function(t, k){ t.classList.toggle("on", k === i); t.classList.toggle("done", k < i);
    t.setAttribute("aria-selected", k === i ? "true" : "false"); t.querySelector(".td-bar i").style.width = "0"; }); }
  function fillBar(i, ms, id){ var bar = tabs[i].querySelector(".td-bar i"), last = Date.now(), done = 0;
    (function tick(){ if(id !== run) return; var now = Date.now(); if(!paused && !document.hidden) done += now - last; last = now;
      bar.style.width = Math.min(100, done / ms * 100) + "%"; if(done < ms) requestAnimationFrame(tick); })(); }
  function fillAll(){ rows.forEach(function(r){ r.classList.add("fill"); }); paper.classList.add("all"); }
  function state(i){
    thread.innerHTML = ""; field.textContent = ""; field.classList.remove("typing"); send.classList.remove("ready");
    stage.classList.remove("pdf"); paper.classList.remove("all"); rows.forEach(function(r){ r.classList.remove("fill"); });
    if(i >= 1){ add("td-time", "Today 9:41 AM"); add("td-bub td-me", esc(OFFER)); add("td-bub td-them", esc(REPLY1)); }
    if(i >= 2){ add("td-time", "Today 9:42 AM"); add("td-bub td-me", "YES"); add("td-bub td-them", REPLY2); stage.classList.add("pdf"); }
  }
  function type(text, id){ field.classList.add("typing"); var p = Promise.resolve();
    text.split("").forEach(function(ch){ p = p.then(function(){ field.textContent += ch; send.classList.add("ready"); return wait(38, id); }); });
    return p.then(function(){ return wait(350, id); }).then(function(){ field.textContent = ""; field.classList.remove("typing"); send.classList.remove("ready"); }); }
  function reply(html, id){ var d = add("td-dots", "<i></i><i></i><i></i>"); return wait(1100, id).then(function(){ d.remove(); add("td-bub td-them", html); }); }
  function play(i, id){
    setTabs(i); state(i); fillBar(i, DUR[i], id); var started = Date.now(), p;
    if(i === 0) p = wait(500, id).then(function(){ add("td-time", "Today 9:41 AM"); return type(OFFER, id); }).then(function(){ add("td-bub td-me", esc(OFFER)); return reply(esc(REPLY1), id); });
    if(i === 1) p = wait(600, id).then(function(){ add("td-time", "Today 9:42 AM"); return type("YES", id); }).then(function(){ add("td-bub td-me", "YES"); return reply(REPLY2, id); });
    if(i === 2){ stage.classList.remove("pdf"); p = wait(400, id).then(function(){ stage.classList.add("pdf"); return wait(700, id); });
      rows.forEach(function(r){ p = p.then(function(){ r.classList.add("fill"); return wait(520, id); }); });
      p = p.then(function(){ paper.classList.add("all"); }); }
    return p.then(function(){ return wait(Math.max(1200, DUR[i] - (Date.now() - started)), id); }).then(function(){ return play((i + 1) % 3, id); });
  }
  function start(i){ run++; play(i, run).catch(function(){}); }
  function showStep(k){ run++; setTabs(k); state(Math.min(k + 1, 2)); if(k === 2) fillAll(); }
  tabs.forEach(function(t, k){ t.addEventListener("click", function(){ if(reduced) return showStep(k); paused = false; $("tdPause").textContent = "Pause"; start(k); }); });
  $("tdPause").addEventListener("click", function(){ paused = !paused; this.textContent = paused ? "Play" : "Pause"; });
  $("tdReplay").addEventListener("click", function(){ paused = false; $("tdPause").textContent = "Pause"; reduced ? showStep(2) : start(0); });
  if(reduced){ showStep(2); return; }
  // Before it scrolls into view, show the finished result (no-JS-ish fallback
  // for crawlers and link previews), then play from the start once seen.
  showStep(2);
  if("IntersectionObserver" in window){ var io = new IntersectionObserver(function(es){ if(es[0].isIntersecting){ io.disconnect(); start(0); } }, {threshold:.35}); io.observe(stage); }
  else start(0);
})();
</script>
"""
