import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, ArrowDownRight, ArrowUpRight, Bell, Check, ChevronDown, CircleHelp,
  Code2, Copy, Download, ExternalLink, FileChartColumnIncreasing, Gauge, Layers3,
  LockKeyhole, Menu, MoreHorizontal, Pause, Play, QrCode, Radio, RefreshCw,
  Search, Settings2, ShieldCheck, Sparkles, TrendingDown, TrendingUp, Wallet, X,
} from 'lucide-react';
import { Area, AreaChart, CartesianGrid, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { QRCodeSVG } from 'qrcode.react';

type Section = 'overview' | 'strategy' | 'kaggle' | 'activity' | 'settings' | 'sz159502';
type SZDashboard = {
  mode: 'demo'; offline: boolean; symbol: string; name: string; market: string; timezone: string; currency: string; feed: string; disclaimer: string; updated_at: string;
  account: { status: string; cash: number; equity: number; buying_power: number; portfolio_value: number; currency: string; initial_equity: number };
  positions: Position[]; strategy: { fast_window: number; slow_window: number; bar_minutes: number; side: string; execution: string; overnight: string };
  risk: { initial_equity: number; per_trade_fraction: number; daily_loss_limit_fraction: number; behavior: string };
  cost: { slippage_bps: number; commission_per_order: number; note: string; is_placeholder: boolean };
  engine: { enabled: boolean; note: string; paused_reason?: string | null };
  equity_curve: { time: string; equity: number; benchmark: number }[];
  orders: { symbol: string; side: string; qty: number; price?: number; status: string; client_order_id?: string; created_at?: string }[];
  unverified: string[];
};
type Position = { symbol: string; qty: number; avg_entry_price: number; current_price: number; market_value: number; unrealized_pl: number; unrealized_plpc: number };
type Dashboard = {
  mode: 'demo' | 'paper'; feed: string; updated_at: string; market_bar_at?: string | null; market_status?: string;
  account: { status: string; cash: number; equity: number; last_equity?: number; buying_power: number; portfolio_value: number; currency: string };
  positions: Position[]; watchlist: string[]; strategy: { fast_window: number; slow_window: number; bar_minutes: number };
  engine: { enabled: boolean; note: string }; equity_curve: { time: string; equity: number; benchmark: number }[];
  orders: { time: string; symbol: string; side: string; qty: number; status: string; demo?: boolean }[];
  metrics: { total_return: number; max_drawdown: number; sharpe: number | null; win_rate: number | null } | null;
};

type SZRealtime = {
  symbol: string; name: string; currency: string; price: number; prev_close: number; open: number; high: number; low: number; volume: number;
  change: number; change_pct: number; sma_fast: number | null; sma_slow: number | null; signal: string; next_open_estimate: number; next_bar_execution: string;
  timestamp: string; is_demo: boolean; is_realtime_placeholder: boolean; note: string;
  account: SZDashboard['account']; positions: Position[]; engine: SZDashboard['engine'];
  risk: { initial_equity: number; daily_drawdown_pct: number; daily_loss_limit_pct: number; can_trade: boolean };
  cost: { slippage_bps: number; commission_per_order: number; note: string; is_placeholder: boolean };
};

const demo: Dashboard = {
  mode: 'demo', feed: '演示数据 · 非实时行情', updated_at: new Date().toISOString(),
  account: { status: 'DEMO', cash: 86799.4, equity: 102480, last_equity: 100000, buying_power: 86799.4, portfolio_value: 102480, currency: 'USD' },
  positions: [
    { symbol: 'AAPL', qty: 20, avg_entry_price: 183.2799, current_price: 219.42, market_value: 4388.4, unrealized_pl: 722.8, unrealized_plpc: 0.1972 },
    { symbol: 'MSFT', qty: 12, avg_entry_price: 422.7758, current_price: 501.55, market_value: 6018.6, unrealized_pl: 945.29, unrealized_plpc: 0.1863 },
    { symbol: 'SPY', qty: 8, avg_entry_price: 557.7116, current_price: 659.2, market_value: 5273.6, unrealized_pl: 811.91, unrealized_plpc: 0.1820 },
  ],
  watchlist: ['SPY', 'QQQ', 'AAPL', 'MSFT', 'NVDA'], strategy: { fast_window: 20, slow_window: 50, bar_minutes: 5 },
  engine: { enabled: false, note: '演示模式；未连接券商' },
  equity_curve: Array.from({ length: 78 }, (_, i) => { const p = i / 77; const minute = 570 + i * 5; return { time: `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`, equity: 100000 + 2480 * p + 180 * Math.sin(p * Math.PI * 6) * Math.sin(p * Math.PI), benchmark: 100000 + 1600 * p + 80 * Math.sin(p * Math.PI * 3) }; }),
  orders: [
    { time: '10:35', symbol: 'AAPL', side: 'BUY', qty: 20, status: 'FILLED', demo: true },
    { time: '09:55', symbol: 'MSFT', side: 'BUY', qty: 12, status: 'FILLED', demo: true },
    { time: '09:40', symbol: 'SPY', side: 'BUY', qty: 8, status: 'FILLED', demo: true },
  ], metrics: { total_return: 0.0248, max_drawdown: -0.0004415, sharpe: null, win_rate: null },
};
const nav: { id: Section; label: string; icon: typeof Activity; index: string }[] = [
  { id: 'overview', label: '总览', icon: Layers3, index: '01' },
  { id: 'sz159502', label: '159502 纸面', icon: Wallet, index: '02' },
  { id: 'strategy', label: '策略实验室', icon: Gauge, index: '03' },
  { id: 'kaggle', label: 'Kaggle Notebook', icon: Code2, index: '04' },
  { id: 'activity', label: '订单记录', icon: Activity, index: '05' },
  { id: 'settings', label: '系统设置', icon: Settings2, index: '06' },
];
const money = (n: number, currency: string = 'USD') => new Intl.NumberFormat(currency === 'CNY' ? 'zh-CN' : 'en-US', { style: 'currency', currency, maximumFractionDigits: 0 }).format(n);
const precise = (n: number, currency: string = 'USD') => new Intl.NumberFormat(currency === 'CNY' ? 'zh-CN' : 'en-US', { style: 'currency', currency, minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n);
const pct = (n: number) => `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;

export default function App() {
  const [section, setSection] = useState<Section>('overview');
  const [data, setData] = useState<Dashboard>(demo);
  const [loading, setLoading] = useState(true);
  const [apiOk, setApiOk] = useState(false);
  const [authChecking, setAuthChecking] = useState(true);
  const [loginRequired, setLoginRequired] = useState(false);
  const [loginError, setLoginError] = useState('');
  const [csrf, setCsrf] = useState('');
  const [publicUrl, setPublicUrl] = useState(import.meta.env.VITE_PUBLIC_URL || window.location.origin);
  const [notice, setNotice] = useState('');
  const [showQr, setShowQr] = useState(false);
  const [mobileMenu, setMobileMenu] = useState(false);
  const [search, setSearch] = useState('');
  const [period, setPeriod] = useState('1D');
  const [copied, setCopied] = useState(false);
  const [code, setCode] = useState('');
  const [szData, setSzData] = useState<SZDashboard | null>(null);
  const [szLoading, setSzLoading] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch('/api/dashboard', { credentials: 'same-origin' });
      if (res.status === 401) { setLoginRequired(true); setApiOk(true); return; }
      if (!res.ok) throw new Error(`API ${res.status}`);
      const body = await res.json() as Dashboard;
      setData(body); setApiOk(true);
    } catch {
      setData(demo); setApiOk(false);
    } finally { setLoading(false); }
  }, []);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const [sessionRes, configRes] = await Promise.all([
          fetch('/api/auth/session', { credentials: 'same-origin' }),
          fetch('/api/public-config', { credentials: 'same-origin' }),
        ]);
        const session = sessionRes.ok ? await sessionRes.json() : null;
        const config = configRes.ok ? await configRes.json() : null;
        if (!alive) return;
        if (session?.csrf_token) setCsrf(session.csrf_token);
        const required = Boolean(session?.login_required && !session?.authenticated);
        setLoginRequired(required);
        if (config?.public_url) setPublicUrl(config.public_url);
        if (!required) await refresh();
      } catch { if (alive) await refresh(); }
      finally { if (alive) setAuthChecking(false); }
    })();
    return () => { alive = false; };
  }, [refresh]);

  useEffect(() => {
    if (authChecking || loginRequired) return;
    const timer = window.setInterval(() => void refresh(), 30_000);
    return () => window.clearInterval(timer);
  }, [authChecking, loginRequired, refresh]);

  const refreshSZ = useCallback(async () => {
    try {
      setSzLoading(true);
      const res = await fetch('/api/sz159502/demo-dashboard', { credentials: 'same-origin' });
      if (!res.ok) throw new Error();
      const body = await res.json() as SZDashboard;
      setSzData(body);
    } catch {
      // 保持离线标注，不假装可用
    } finally { setSzLoading(false); }
  }, []);

  useEffect(() => {
    if (authChecking || loginRequired) return;
    void refreshSZ();
  }, [authChecking, loginRequired, refreshSZ]);

  useEffect(() => {
    if (section !== 'kaggle' || code) return;
    void fetch('/api/kaggle-code', { credentials: 'same-origin' })
      .then((res) => res.ok ? res.text() : Promise.reject())
      .then(setCode).catch(() => setCode('# Kaggle script is unavailable in this preview.\n# No credentials are embedded.'));
  }, [section, code]);

  const positions = useMemo(() => data.positions.filter((p) => p.symbol.toLowerCase().includes(search.toLowerCase())), [data.positions, search]);
  const lastUpdated = new Date(data.updated_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', timeZone: 'America/Los_Angeles' });
  const goTo = (next: Section) => { setSection(next); setMobileMenu(false); };

  const submitLogin = async (password: string) => {
    setLoginError('');
    try {
      const res = await fetch('/api/auth/login', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }) });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || '登录失败');
      setCsrf(body.csrf_token || ''); setLoginRequired(false); setLoading(true); await refresh();
    } catch (error) { setLoginError(error instanceof Error ? error.message : '暂时无法登录'); }
  };

  const copyCode = async () => {
    try {
      let full = code;
      if (!full) { const res = await fetch('/api/kaggle-code', { credentials: 'same-origin' }); if (!res.ok) throw new Error(); full = await res.text(); }
      await navigator.clipboard.writeText(full); setCopied(true); setNotice('Kaggle Python 代码已复制');
      window.setTimeout(() => setCopied(false), 2200);
    } catch { setNotice('复制失败：请确认 HTTPS 与剪贴板权限'); }
    window.setTimeout(() => setNotice(''), 3200);
  };

  const controlEngine = async () => {
    if (!csrf || data.mode !== 'paper') { setNotice('当前为演示模式；不会连接券商或提交订单'); window.setTimeout(() => setNotice(''), 3000); return; }
    const action = data.engine.enabled ? 'stop' : 'start';
    if (action === 'start' && !window.confirm('确认只在 Alpaca Paper 模拟账户启用策略？不会提交真实订单。')) return;
    try {
      const send = (acknowledgeRisk: boolean) => fetch(`/api/engine/${action}`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' },
        body: action === 'start' ? JSON.stringify({ acknowledge_risk: acknowledgeRisk }) : undefined,
      });
      let res = await send(false);
      let body = await res.json();
      if (!res.ok && action === 'start' && String(body.detail || '').includes('回撤熔断')) {
        if (!window.confirm('当日回撤已触及 2% 熔断。确认已检查账户，并重置当日风险基准后重新启用 Paper 策略？')) return;
        res = await send(true);
        body = await res.json();
      }
      if (!res.ok) throw new Error(body.detail || '操作失败');
      setNotice(action === 'start' ? '纸面策略已启用' : '已暂停新订单'); await refresh();
    } catch (error) { setNotice(error instanceof Error ? error.message : '请求失败，策略保持暂停'); }
    window.setTimeout(() => setNotice(''), 3500);
  };

  if (authChecking) return <BootScreen />;
  if (loginRequired) return <LoginScreen error={loginError} onSubmit={submitLogin} />;

  const currentLabel = nav.find((item) => item.id === section)?.label || '总览';
  return <div className="app-shell">
    <aside className={`sidebar ${mobileMenu ? 'sidebar-open' : ''}`}>
      <div className="brand-lockup"><div className="brand-symbol"><span/><span/><span/></div><div><div className="brand-name">ORBIT<span> / </span>QUANT</div><div className="brand-caption">RESEARCH TERMINAL <b>01</b></div></div><button className="icon-button sidebar-close" aria-label="关闭菜单" onClick={() => setMobileMenu(false)}><X size={18}/></button></div>
      <div className="workspace-chip"><div className="workspace-avatar">Q</div><div className="workspace-meta"><strong>个人工作台</strong><span>CPU · KAGGLE LAB</span></div><ChevronDown size={14}/></div>
      <div className="nav-kicker">工作区 <span>WORKSPACE</span></div><nav className="primary-nav" aria-label="主导航">{nav.map(({id,label,icon:Icon,index})=><button key={id} className={`nav-item ${section===id?'active':''}`} onClick={()=>goTo(id)}><Icon size={17}/><span>{label}</span><small>{index}</small></button>)}</nav>
      <div className="sidebar-bottom"><div className="mini-status"><span className="status-dot"/><span>数据源</span><strong>{data.mode==='paper'?'IEX · PAPER':'演示环境'}</strong></div><div className="sidebar-note"><div className="note-icon"><ShieldCheck size={15}/></div><p>纸面交易<br/><span>不会触及真实资金</span></p><span className="note-lock"><LockKeyhole size={13}/></span></div><div className="sidebar-footer"><span>QUANT DESK <i>v0.1</i></span><span>US EQUITIES</span></div></div>
    </aside>

    <main className="main-area">
      <header className="topbar"><button className="icon-button mobile-menu-button" aria-label="打开菜单" onClick={()=>setMobileMenu(true)}><Menu size={19}/></button><div className="breadcrumb"><span>工作区</span><i>/</i><strong>{currentLabel}</strong></div><div className="topbar-right"><div className="market-clock"><span className="clock-pulse"/> US MARKET <b>5 MIN BARS</b></div><button className="icon-button notification-button" aria-label="通知"><Bell size={17}/><i/></button><button className="avatar-button" aria-label="用户菜单">Q</button></div></header>
      <div className="content-wrap">
        <div className="page-heading"><div><div className="eyebrow"><span className="eyebrow-line"/> QUANTITATIVE STUDIO <span className="eyebrow-index">/ 2026</span></div><h1>{section==='overview'?<>市场有节奏。<em>策略要有边界。</em></>:currentLabel}</h1><p className="page-subtitle">把研究、风险与执行放在同一张桌面上。<span className="subtitle-divider">—</span>仅供纸面交易与量化研究。</p></div><div className="heading-actions"><div className={`environment-pill ${data.mode}`}><span className="environment-dot"/><span>{data.mode==='paper'?'PAPER LINK':'DEMO PREVIEW'}</span><b>{data.mode==='paper'?'IEX':'SAMPLE'}</b></div><button className="button button-quiet qr-open" onClick={()=>setShowQr(true)}><QrCode size={15}/> <span>扫码访问</span></button><button className="button button-primary" onClick={()=>void controlEngine()}>{data.engine.enabled?<Pause size={15} fill="currentColor"/>:<Play size={15} fill="currentColor"/>}{data.engine.enabled?'暂停策略':'纸面策略'}<span className="button-shortcut">↗</span></button></div></div>
        <div className="notice-strip"><div className="notice-mark"><Sparkles size={15}/></div><span><strong>{data.mode==='demo'?'演示数据 · 非实时行情':'Alpaca IEX · 单一交易所行情'}</strong><i>—</i>{data.mode==='demo'?'此视图仅展示界面，不代表真实绩效。':'模拟账户不会使用真实资金。'}</span><button onClick={()=>goTo('strategy')}>查看策略规则 <ArrowUpRight size={14}/></button></div>
        {section==='overview'&&<Overview data={data} positions={positions} period={period} setPeriod={setPeriod} search={search} setSearch={setSearch} onRefresh={()=>void refresh()} loading={loading} apiOk={apiOk} onNavigate={goTo} szData={szData}/>}
        {section==='sz159502'&&<SZ159502Page data={szData} loading={szLoading} onRefresh={()=>void refreshSZ()} csrf={csrf} />}
        {section==='strategy'&&<StrategyPage data={data} szData={szData}/>}
        {section==='kaggle'&&<KagglePage code={code} copied={copied} onCopy={()=>void copyCode()}/>}
        {section==='activity'&&<ActivityPage data={data}/>}
        {section==='settings'&&<SettingsPage data={data} publicUrl={publicUrl} szData={szData}/>}
        <footer className="content-footer"><span>QUANT DESK <b>·</b> PAPER FIRST</span><span><i className="footer-live-dot"/> {apiOk?'API 已连接':'本地预览'} <b>·</b> 更新于 {lastUpdated} PT</span><span>仅供研究，不构成投资建议</span></footer>
      </div>
    </main>
    {showQr&&<div className="modal-backdrop" role="presentation" onClick={()=>setShowQr(false)}><div className="qr-modal" role="dialog" aria-modal="true" aria-labelledby="qr-title" onClick={(e)=>e.stopPropagation()}><button className="icon-button modal-close" onClick={()=>setShowQr(false)} aria-label="关闭"><X size={18}/></button><div className="qr-modal-eyebrow"><QrCode size={14}/> SECURE ACCESS</div><h2 id="qr-title">带上你的<br/><em>量化工作台。</em></h2><p>使用手机相机扫描二维码。请仅在你信任的设备上登录个人纸面账户。</p><div className="qr-code-wrap"><QRCodeSVG value={publicUrl} size={180} bgColor="#f2f0e9" fgColor="#111819" level="M" includeMargin/></div><div className="qr-url"><span>{publicUrl}</span><ExternalLink size={14}/></div><div className="qr-hint"><LockKeyhole size={13}/> PAPER ONLY · HTTPS REQUIRED</div></div></div>}
    {notice&&<div className="toast" role="status"><span className="toast-check"><Check size={14}/></span>{notice}</div>}<div className="ambient-glow" aria-hidden="true"/>
  </div>;
}

function BootScreen(){return <div className="auth-screen"><div className="auth-card boot-card"><div className="brand-symbol"><span/><span/><span/></div><div className="auth-kicker">QUANT DESK · SECURE BOOT</div><div className="boot-loader"><i/></div><p>正在检查服务与访问策略…</p></div></div>;}
function LoginScreen({error,onSubmit}:{error:string;onSubmit:(password:string)=>Promise<void>}){const [password,setPassword]=useState('');const [busy,setBusy]=useState(false);return <div className="auth-screen"><form className="auth-card" onSubmit={async(e)=>{e.preventDefault();setBusy(true);await onSubmit(password);setBusy(false);}}><div className="brand-symbol"><span/><span/><span/></div><div className="auth-kicker">PRIVATE QUANT WORKSPACE</div><h1>欢迎回来。</h1><p>请验证身份以访问你的个人 Paper 交易工作台。</p><label className="auth-label" htmlFor="auth-password">访问口令</label><div className="auth-input"><LockKeyhole size={16}/><input id="auth-password" type="password" autoComplete="current-password" value={password} onChange={(e)=>setPassword(e.target.value)} placeholder="输入服务器配置的口令" required/></div>{error&&<div className="auth-error" role="alert">{error}</div>}<button className="button button-primary auth-submit" disabled={busy}>{busy?'验证中…':'安全登录'} <ArrowUpRight size={15}/></button><div className="auth-foot"><ShieldCheck size={13}/>仅 Paper 模拟账户 · 不会触及真实资金</div></form></div>;}

function Overview({data,positions,period,setPeriod,search,setSearch,onRefresh,loading,apiOk,onNavigate,szData:_szData}:{data:Dashboard;positions:Position[];period:string;setPeriod:(p:string)=>void;search:string;setSearch:(s:string)=>void;onRefresh:()=>void;loading:boolean;apiOk:boolean;onNavigate:(s:Section)=>void;szData:SZDashboard|null}){
  const m=data.metrics;const dailyReturn=data.account.last_equity&&data.account.last_equity>0?(data.account.equity/data.account.last_equity-1)*100:null;const total=data.mode==='demo'?(m?.total_return??0)*100:(dailyReturn??0);const dd=data.mode==='demo'?Math.abs((m?.max_drawdown??0)*100):Math.max(0,-(dailyReturn??0));const returnAvailable=data.mode==='demo'||dailyReturn!==null;
  return <>
    <section className="metric-grid"><MetricCard label="账户权益" value={money(data.account.equity)} sub={data.mode==='paper'?'Alpaca Paper · USD':'演示账户 · USD'} icon={Wallet} badge={data.mode==='paper'?'PAPER EQUITY':'DEMO EQUITY'}/><MetricCard label={data.mode==='demo'?'演示区间收益':'账户日变动'} value={returnAvailable?pct(total):'—'} sub={data.mode==='demo'?'合成演示 · 非真实绩效':'相对 Alpaca 上一交易日权益'} icon={total>=0?TrendingUp:TrendingDown} positive={total>=0} badge={data.mode==='demo'?'DEMO RETURN':'ACCOUNT CHANGE'}/><MetricCard label={data.mode==='demo'?'样例最大回撤':'日权益回落'} value={data.mode==='demo'?`−${dd.toFixed(2)}%`:dailyReturn===null?'—':`−${dd.toFixed(2)}%`} sub={data.mode==='demo'?'合成数据 · 非预测':'相对上日权益 · 非日内高水位'} icon={ArrowDownRight} warning badge="DRAWDOWN REF"/><MetricCard label="Sharpe 比率" value={m?.sharpe?.toFixed(2)??'—'} sub={data.mode==='demo'?'合成演示指标':'Paper 历史序列未储存'} icon={Gauge} badge="RISK-ADJUSTED"/></section>
    <section className="dashboard-grid"><div className="panel chart-panel"><div className="panel-header chart-header"><div><div className="panel-kicker"><span className="kicker-dot green"/> EQUITY CURVE <span className="panel-separator">/</span>组合权益</div><h2>策略轨迹<span className="chart-period-label">· {data.mode==='demo'?(period==='1D'?'今日演示':period==='1W'?'近一周':'近一月'):'Paper 账户快照'}</span></h2></div><div className="chart-actions">{data.mode==='demo'&&<div className="segmented-control">{['1D'].map((x)=><button key={x} className={period===x?'selected':''} onClick={()=>setPeriod(x)}>{x}</button>)}</div>}<button className="icon-button refresh-button" aria-label="刷新" onClick={onRefresh}><RefreshCw size={15} className={loading?'spin':''}/></button><button className="icon-button more-button" aria-label="更多"><MoreHorizontal size={18}/></button></div></div><div className="chart-summary"><strong>{money(data.account.equity)}</strong><span className={`chart-change ${total>=0?'positive':'negative'}`}>{total>=0?<ArrowUpRight size={14}/>:<ArrowDownRight size={14}/>} {returnAvailable?pct(total):'—'} <i>{data.mode==='demo'?'演示区间':'账户日变动'}</i></span><span className="chart-updated">{apiOk?'API 同步':'本地演示'}</span></div><div className="chart-wrap">{data.equity_curve.length>0?<ResponsiveContainer width="100%" height="100%"><AreaChart data={data.equity_curve} margin={{top:18,right:8,left:-17,bottom:0}}><defs><linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#a8f06a" stopOpacity={.22}/><stop offset="96%" stopColor="#a8f06a" stopOpacity={0}/></linearGradient></defs><CartesianGrid stroke="#ffffff0b" vertical={false}/><XAxis dataKey="time" axisLine={false} tickLine={false} tick={{fill:'#657170',fontSize:10,fontFamily:'DM Mono'}} interval="preserveStartEnd" minTickGap={42}/><YAxis axisLine={false} tickLine={false} tick={{fill:'#657170',fontSize:10,fontFamily:'DM Mono'}} tickFormatter={(v:number)=>`$${Math.round(v/1000)}k`} domain={['dataMin - 500','dataMax + 700']}/><Tooltip content={<ChartTooltip/>}/><Area type="monotone" dataKey="equity" stroke="#a8f06a" strokeWidth={2} fill="url(#equityFill)" activeDot={{r:4,strokeWidth:0,fill:'#d5ffae'}} isAnimationActive animationDuration={900}/><Line type="monotone" dataKey="benchmark" stroke="#7581e5" strokeWidth={1.4} strokeDasharray="4 5" dot={false}/></AreaChart></ResponsiveContainer>:<div className="chart-empty"><Activity size={19}/><strong>尚无 Paper 权益历史曲线</strong><span>当前显示券商账户快照；不会用演示数据替代 Paper 记录。</span></div>}</div><div className="chart-legend"><span><i className="legend-line legend-equity"/>策略权益</span><span><i className="legend-line legend-benchmark"/>Buy & Hold 基准</span><span className="legend-footnote">回测与模拟盘成交可能不同</span></div></div>
      <div className="panel risk-panel"><div className="panel-header"><div><div className="panel-kicker"><span className="kicker-dot amber"/> RISK GUARD <span className="panel-separator">/</span>风险边界</div><h2>护栏状态</h2></div><ShieldCheck size={18} className="panel-header-icon"/></div><div className="risk-status-row"><span className="risk-check"><Check size={14}/></span><div><strong>{data.mode==='demo'?'演示状态':data.engine.enabled?'策略运行中':data.engine.note.includes('熔断')?'熔断暂停':'策略已暂停'}</strong><span>{data.mode==='paper'?data.engine.note:'演示模式 · 未接入账户'}</span></div><span className="risk-state-tag">{data.mode==='paper'?(data.engine.enabled?'ARMED':'PAUSED'):'PREVIEW'}</span></div><div className="risk-meter-label"><span>当日回撤熔断</span><strong>2.00%</strong></div><div className="risk-meter"><span style={{width:`${Math.min(dd/2*100,100)}%`}}/></div><div className="risk-meter-scale"><span>{data.mode==='demo'?`样例回撤 ${dd.toFixed(2)}%`:returnAvailable?`账户日回落 ${dd.toFixed(2)}%`:'账户日回落 —'}</span><span>暂停阈值</span></div><div className="risk-rule-list"><div><span className="rule-icon"><Layers3 size={14}/></span><span>单标的仓位上限</span><strong>10%</strong></div><div><span className="rule-icon"><Gauge size={14}/></span><span>总持仓上限</span><strong>50%</strong></div><div><span className="rule-icon"><LockKeyhole size={14}/></span><span>交易环境</span><strong>仅 PAPER</strong></div></div><div className="risk-footer"><CircleHelp size={13}/><span>触及熔断后停止新订单，需人工确认。</span></div></div></section>
    <section className="bottom-grid"><div className="panel positions-panel"><div className="panel-header table-heading"><div><div className="panel-kicker"><span className="kicker-dot blue"/> OPEN POSITIONS <span className="panel-separator">/</span>当前持仓</div><h2>组合构成 <span className="count-chip">{positions.length}</span></h2></div><div className="table-tools"><label className="search-field"><Search size={14}/><input value={search} onChange={(e)=>setSearch(e.target.value)} placeholder="查找代码" aria-label="查找代码"/></label></div></div><div className="table-scroll"><table><thead><tr><th>标的</th><th>数量</th><th>平均成本</th><th>最新参考</th><th>市值</th><th>未实现盈亏</th></tr></thead><tbody>{positions.map(p=><tr key={p.symbol}><td><div className="ticker-cell"><span className={`ticker-mark ${p.symbol==='SPY'?'ticker-purple':''}`}>{p.symbol[0]}</span><div><strong>{p.symbol}</strong><small>{p.symbol==='AAPL'?'Apple Inc.':p.symbol==='MSFT'?'Microsoft Corp.':p.symbol==='SPY'?'SPDR S&P 500 ETF':'US EQUITY'}</small></div></div></td><td className="mono">{p.qty}</td><td className="mono">{precise(p.avg_entry_price)}</td><td className="mono">{precise(p.current_price)}</td><td className="mono">{money(p.market_value)}</td><td><span className={`pnl ${p.unrealized_pl>=0?'positive':'negative'}`}>{p.unrealized_pl>=0?'+':''}{precise(p.unrealized_pl)} <small>{pct(p.unrealized_plpc*100)}</small></span></td></tr>)}{positions.length===0&&<tr><td colSpan={6} className="empty-cell">没有匹配的持仓</td></tr>}</tbody></table></div><div className="panel-bottom-link"><span>行情参考：{data.feed}</span><button onClick={()=>onNavigate('activity')}>订单记录 <ArrowUpRight size={13}/></button></div></div>
      <div className="panel signals-panel"><div className="panel-header"><div><div className="panel-kicker"><span className="kicker-dot orange"/> SIGNAL FEED <span className="panel-separator">/</span>最近活动</div><h2>订单与信号</h2></div><button className="text-icon-button" onClick={()=>onNavigate('activity')}>全部 <ArrowUpRight size={13}/></button></div><div className="signal-list">{data.orders.slice(0,4).map((o,i)=><div className="signal-item" key={`${o.symbol}-${i}`}><div className={`signal-symbol ${o.side==='BUY'?'buy':'sell'}`}>{o.side==='BUY'?<ArrowDownRight size={15}/>:<ArrowUpRight size={15}/>}</div><div className="signal-content"><div><strong>{o.symbol}</strong><span className={`side-tag ${o.side.toLowerCase()}`}>{o.side==='BUY'?'买入':'卖出'}</span></div><small>{o.qty||'—'} 股 <i>·</i>{o.demo?'演示样例':'Paper 订单'}</small></div><div className="signal-meta"><span className="filled-dot"/>{o.status}<small>{o.time}</small></div></div>)}{data.orders.length===0&&<div className="empty-activity">暂无已确认订单</div>}</div><div className="signal-foot"><Radio size={13}/><span>仅处理已完成的 5 分钟 bar</span><span className="signal-time">状态以 API 为准</span></div></div></section>
  </>;
}
function MetricCard({label,value,sub,icon:Icon,positive,warning,delta,badge}:{label:string;value:string;sub:string;icon:typeof Activity;positive?:boolean;warning?:boolean;delta?:string;badge:string}){return <article className={`metric-card ${warning?'metric-warning':''}`}><div className="metric-card-top"><div className="metric-label">{label}</div><div className="metric-icon"><Icon size={16}/></div></div><div className="metric-value-row"><strong className={positive?'value-positive':''}>{value}</strong>{delta&&<span className="metric-delta"><ArrowUpRight size={12}/>{delta}</span>}</div><div className="metric-foot"><span>{sub}</span><i>{badge}</i></div><span className="metric-corner"/></article>;}
function ChartTooltip({active,payload,label}:{active?:boolean;payload?:{name:string;value:number;color:string}[];label?:string}){if(!active||!payload?.length)return null;return <div className="chart-tooltip"><span>{label}</span>{payload.map(item=><div key={item.name}><i style={{background:item.color}}/>{item.name==='equity'?'策略权益':'买入并持有'}<strong>{precise(item.value)}</strong></div>)}</div>;}
function StrategyPage({data,szData}:{data:Dashboard;szData:SZDashboard|null}){return <div className="subpage-grid"><section className="panel strategy-hero"><div className="panel-kicker"><span className="kicker-dot green"/> STRATEGY SPECIFICATION <span className="panel-separator">/</span>策略说明</div><div className="strategy-title-row"><div><h2>均线交叉<span> / SMA CROSS</span></h2><p>透明、可复现的长仓基线策略；信号只依据已完成 bar。</p></div><div className="strategy-status"><span className="status-dot"/>{data.engine.enabled?'运行中':'已暂停'}</div></div><div className="strategy-steps"><div className="strategy-step"><small>01 · OBSERVE</small><strong>5 分钟 OHLCV</strong><span>仅完整 bar</span></div><div className="step-connector"/><div className="strategy-step"><small>02 · CALCULATE</small><strong>SMA {data.strategy.fast_window}/{data.strategy.slow_window}</strong><span>深圳 09:30-11:30 / 13:00-15:00 + 美股 09:30-16:00 ET</span></div><div className="step-connector"/><div className="strategy-step"><small>03 · EXECUTE</small><strong>下一根可交易 K 开盘成交</strong><span>上穿买入 · 下穿退出 · 允许隔夜</span></div></div><div className="strategy-disclaimer"><ShieldCheck size={16}/><span>不做空、不加杠杆、不追补停机信号。恢复前需先对账；异常会暂停新订单。</span></div></section><section className="panel config-panel"><div className="panel-header"><div><div className="panel-kicker"><span className="kicker-dot amber"/> PARAMETERS <span className="panel-separator">/</span>运行参数</div><h2>策略边界</h2></div><Settings2 size={17} className="panel-header-icon"/></div><ConfigRow title="观察清单" sub="最多 30 个代码" value={data.watchlist.join(' · ')}/><ConfigRow title="单标的仓位" sub="按权益估算、整股向下取整" value="≤ 10%"/><ConfigRow title="总持仓上限" sub="禁止杠杆与做空" value="≤ 50%"/><ConfigRow title="当日回撤熔断" sub="人工确认后方可恢复" value="2.00%" amber/><ConfigRow title="回测成本假设" sub="仅用于历史研究" value="5 bps / side（占位值，非真实费率）"/><div className="config-note"><CircleHelp size={14}/>参数修改需由后端校验。</div>{szData&&<div className="config-note" style={{marginTop:12}}><CircleHelp size={14}/>159502：¥{szData.risk.initial_equity.toLocaleString()} 初始 · 单笔≤{szData.risk.per_trade_fraction*100}% · 日熔断 {szData.risk.daily_loss_limit_fraction*100}% 后止买并尝试卖出 · 成本：{szData.cost.note}</div>}</section><section className="panel validation-panel"><div className="panel-header"><div><div className="panel-kicker"><span className="kicker-dot blue"/> MODEL VALIDATION <span className="panel-separator">/</span>回测方法</div><h2>时间序列验证</h2></div><FileChartColumnIncreasing size={17} className="panel-header-icon"/></div><div className="validation-bar"><div><span>开发 / 观察段</span><strong>70%</strong></div><div><span>留出验证段</span><strong>30%</strong></div></div><div className="validation-fill"><span/><i/></div><div className="validation-details"><div><span>切分方式</span><strong>按时间顺序</strong></div><div><span>基准</span><strong>同标的 Buy & Hold</strong></div><div><span>关键指标</span><strong>收益 · 回撤 · Sharpe</strong></div></div><p className="small-note">留出验证段不用于调参；数据缺失的代码会跳过并说明。159502 因未获授权五分钟历史，仅离线 Demo，不做真实回测。</p></section></div>;}
function ConfigRow({title,sub,value,amber}:{title:string;sub:string;value:string;amber?:boolean}){return <div className="config-row"><div><strong>{title}</strong><small>{sub}</small></div><strong className={`config-value ${amber?'config-amber':''}`}>{value}</strong></div>;}
function KagglePage({code,copied,onCopy}:{code:string;copied:boolean;onCopy:()=>void}){const lines=code.split('\n');return <div className="kaggle-layout"><section className="kaggle-intro"><div className="eyebrow"><span className="eyebrow-line"/> CPU NOTEBOOK <span className="eyebrow-index">/ KAGGLE</span></div><h2>研究在 Kaggle。<br/><em>代码由你掌握。</em></h2><p>复制自包含 Python 回测脚本，或下载可导入 Kaggle 的 Notebook。无需 GPU；不含任何交易密钥。</p><div className="code-actions"><button className="button button-primary" onClick={onCopy}>{copied?<Check size={15}/>:<Copy size={15}/>} {copied?'已复制完整代码':'复制完整 Python'}</button><a className="button button-quiet" href="/api/kaggle-notebook" download="quant_research.ipynb"><Download size={15}/> 下载 .ipynb</a></div><div className="notebook-facts"><div><span>RUNTIME</span><strong>CPU only</strong></div><div><span>TIMEFRAME</span><strong>5 min · RTH</strong></div><div><span>VALIDATION</span><strong>70 / 30 split</strong></div></div><div className="kaggle-data-warning"><div className="warning-mark">!</div><div><strong>先在 Kaggle 添加数据集</strong><span>数据时区须核实；缺失 ticker 会跳过。合成样例不代表真实历史绩效。</span></div></div></section><section className="code-window"><div className="code-window-top"><div className="window-dots"><i/><i/><i/></div><div className="file-tab"><Code2 size={13}/> quant_research.py <span>PYTHON</span></div><button className="icon-button code-copy-icon" onClick={onCopy} aria-label="复制代码">{copied?<Check size={15}/>:<Copy size={15}/>}</button></div><div className="code-body"><div className="line-numbers">{lines.slice(0,28).map((_,i)=><span key={i}>{String(i+1).padStart(2,'0')}</span>)}</div><pre><code>{(lines.slice(0,28).join('\n')||'# 正在加载完整研究脚本…').split('\n').map((line,i)=><span key={i} className={line.trim().startsWith('#')?'code-comment':line.includes('def ')||line.includes('class ')?'code-keyword':''}>{line||' '}{'\n'}</span>)}</code></pre></div><div className="code-window-bottom"><span><i className="code-ready-dot"/>{code?`${lines.length} 行 · 展示前 28 行`:'加载代码…'}</span><button onClick={onCopy}>复制全部 <Copy size={12}/></button></div></section></div>;}
function ActivityPage({data}:{data:Dashboard}){return <section className="panel full-panel"><div className="panel-header"><div><div className="panel-kicker"><span className="kicker-dot orange"/> ORDER JOURNAL <span className="panel-separator">/</span>执行记录</div><h2>最近订单 <span className="count-chip">{data.orders.length}</span></h2></div><span className="feed-badge">{data.mode==='paper'?'ALPACA PAPER':'DEMO FIXTURE'}</span></div><div className="activity-table-wrap"><table><thead><tr><th>时间</th><th>标的</th><th>方向</th><th>数量</th><th>状态</th><th>执行环境</th></tr></thead><tbody>{data.orders.map((o,i)=><tr key={`${o.symbol}-${i}`}><td className="mono">{o.time}</td><td><strong>{o.symbol}</strong></td><td><span className={`side-tag ${o.side.toLowerCase()}`}>{o.side==='BUY'?'买入':'卖出'}</span></td><td className="mono">{o.qty||'—'}</td><td><span className="order-status"><i/>{o.status}</span></td><td>{o.demo?'演示样例':'Paper Only'}</td></tr>)}</tbody></table>{data.orders.length===0&&<div className="blank-state"><Activity size={22}/><strong>暂无订单记录</strong><span>确认后的 Paper 订单会出现在这里。</span></div>}</div><div className="table-bottom-note"><ShieldCheck size={14}/>Paper 状态为券商提交回报，持仓以账户快照为准；不连接 Live API。</div></section>;}
function SettingsPage({data,publicUrl,szData}:{data:Dashboard;publicUrl:string;szData:SZDashboard|null}){return <div className="settings-layout"><section className="panel settings-card"><div className="panel-kicker"><span className="kicker-dot blue"/> ENVIRONMENT <span className="panel-separator">/</span>运行环境</div><h2>访问与服务</h2><SettingLine title="运行模式" sub="真实部署由后端显式配置" value={data.mode==='paper'?'PAPER':'DEMO'}/><SettingLine title="市场数据" sub="Paper 模式读取 IEX 单一交易所 feed" value={data.feed}/><SettingLine title="公开地址" sub="二维码编码此 HTTPS 地址" value={publicUrl}/><SettingLine title="计算环境" sub="Notebook 不使用 GPU" value="CPU ONLY"/>{szData&&<><SettingLine title="159502 市场" sub="深圳 09:30-11:30 / 13:00-15:00 · 允许隔夜" value={`${szData.symbol} · ${szData.market} · ${szData.timezone}`}/><SettingLine title="159502 初始权益" sub="单标的纸面模拟，CNY" value={`¥${szData.account.initial_equity.toLocaleString()} · ¥${szData.account.cash.toLocaleString()} 可用`}/><SettingLine title="159502 成本" sub="可配置占位值，非真实费率" value={`${szData.cost.slippage_bps} bps / ${szData.cost.commission_per_order} · ${szData.cost.note}`}/></>}</section><section className="panel settings-card"><div className="panel-kicker"><span className="kicker-dot amber"/> SAFETY <span className="panel-separator">/</span>安全约定</div><h2>凭证与风控</h2><SecurityNote icon={LockKeyhole} title="密钥只存在服务器端" text="TONGHUASHUN_API_KEY 仅服务端环境变量，前端/浏览器/Notebook/GitHub 不得保存；Alpaca 亦同。"/><SecurityNote icon={ShieldCheck} title="Live 交易路径不存在" text="应用拒绝非 Paper 端点，不包含真实下单模式；159502 仅纸面模拟。"/><SecurityNote icon={RefreshCw} title="重启先对账" text="停机期间旧信号不补发；日回撤熔断后止新买并尝试卖出，需人工确认后恢复。"/>{szData&&<SecurityNote icon={Activity} title="159502 未验证事项" text={szData.unverified.join('；')} />}</section></div>;}

function SZLiveTicker(){
  const [tick, setTick] = useState<SZRealtime | null>(null);
  const [flash, setFlash] = useState<'up'|'down'|null>(null);
  const prevRef = useRef<number | null>(null);
  useEffect(()=>{
    let alive = true;
    const fetchTick = async () => {
      try{
        const res = await fetch('/api/sz159502/realtime', { credentials:'same-origin' });
        if(!res.ok) return;
        const body = await res.json() as SZRealtime;
        if(!alive) return;
        if(prevRef.current !== null){
          if(body.price > prevRef.current) { setFlash('up'); setTimeout(()=>setFlash(null), 600); }
          else if(body.price < prevRef.current) { setFlash('down'); setTimeout(()=>setFlash(null), 600); }
        }
        prevRef.current = body.price;
        setTick(body);
      }catch{}
    };
    void fetchTick();
    const id = window.setInterval(fetchTick, 1500);
    return ()=>{ alive=false; window.clearInterval(id); };
  },[]);
  if(!tick) return <div className="panel" style={{padding:16, background:'linear-gradient(135deg,#081214,#10282c)', border:'1px solid #1c4a52'}}><div style={{display:'flex',alignItems:'center',gap:10,color:'#8ea096'}}><Radio size={14} className="spin"/> 正在连接离线合成脉冲（明确非实时）…</div></div>;
  const up = tick.change >= 0;
  return <div className={`panel sz-ticker-panel ${flash ? `flash-${flash}`:''}`} style={{padding:0, overflow:'hidden', border:'1px solid #1c4a52', background:'linear-gradient(180deg,#0a1e22,#081214)'}}>
    <div style={{display:'grid', gridTemplateColumns:'1.6fr 1fr', gap:0}}>
      <div style={{padding:'18px 20px', borderRight:'1px solid #143035'}}>
        <div style={{font:'11px ui-monospace', color:'#7fb0b6', letterSpacing:.6}}>{tick.symbol} · {tick.name} <span style={{color:'#e3a857',border:'1px solid #3a3a1a',background:'#2a2410',padding:'1px 6px',borderRadius:999,marginLeft:6}}>合成脉冲 · 非实时 · {tick.note}</span></div>
        <div style={{display:'flex',alignItems:'baseline',gap:12, marginTop:6}}>
          <div style={{fontSize:42, fontWeight:900, letterSpacing:-1, color: up ? '#a8f06a' : '#ff6b6b', lineHeight:1}}>{tick.price.toFixed(4)} <span style={{fontSize:14, fontWeight:600, color:'#8ea096'}}>{tick.currency}</span></div>
          <div style={{display:'flex',alignItems:'center',gap:6, background: up ? 'rgba(168,240,106,.12)' : 'rgba(255,107,107,.12)', border:`1px solid ${up?'#2a4a1a':'#4a2a2a'}`, borderRadius:999, padding:'6px 10px'}}>
            {up ? <ArrowUpRight size={14} color="#a8f06a"/> : <ArrowDownRight size={14} color="#ff6b6b"/>}
            <span style={{fontWeight:700, color: up?'#a8f06a':'#ff6b6b'}}>{tick.change>=0?'+':''}{tick.change.toFixed(4)} ({tick.change_pct>=0?'+':''}{tick.change_pct.toFixed(2)}%)</span>
          </div>
          <span style={{font:'11px ui-monospace', color:'#8ea096'}}>{new Date(tick.timestamp).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit', hour12:false})} · 上海</span>
        </div>
        <div style={{display:'grid', gridTemplateColumns:'repeat(4,1fr)', gap:10, marginTop:12}}>
          <div style={{background:'#0f262a',border:'1px solid #1a3a3e',borderRadius:10,padding:'8px 10px'}}><div style={{font:'10px ui-monospace',color:'#8ea096'}}>今开</div><div style={{fontWeight:700}}>{tick.open.toFixed(4)}</div></div>
          <div style={{background:'#0f262a',border:'1px solid #1a3a3e',borderRadius:10,padding:'8px 10px'}}><div style={{font:'10px ui-monospace',color:'#8ea096'}}>最高</div><div style={{fontWeight:700}}>{tick.high.toFixed(4)}</div></div>
          <div style={{background:'#0f262a',border:'1px solid #1a3a3e',borderRadius:10,padding:'8px 10px'}}><div style={{font:'10px ui-monospace',color:'#8ea096'}}>最低</div><div style={{fontWeight:700}}>{tick.low.toFixed(4)}</div></div>
          <div style={{background:'#0f262a',border:'1px solid #1a3a3e',borderRadius:10,padding:'8px 10px'}}><div style={{font:'10px ui-monospace',color:'#8ea096'}}>成交量</div><div style={{fontWeight:700}}>{tick.volume.toLocaleString()}</div></div>
        </div>
        <div style={{marginTop:10, display:'flex', gap:8, flexWrap:'wrap', font:'11px ui-monospace', color:'#9fb0b4'}}>
          <span style={{background:'#132a2e',border:'1px solid #1c4a52',padding:'4px 8px',borderRadius:999}}>SMA{tick.sma_fast!=null?` 快:${tick.sma_fast.toFixed(4)}`:''}</span>
          <span style={{background:'#132a2e',border:'1px solid #1c4a52',padding:'4px 8px',borderRadius:999}}>SMA{tick.sma_slow!=null?` 慢:${tick.sma_slow.toFixed(4)}`:''}</span>
          <span style={{background: tick.signal==='BUY'?'rgba(168,240,106,.12)':tick.signal==='SELL'?'rgba(255,107,107,.12)':'#132a2e',border:'1px solid #1c4a52',padding:'4px 8px',borderRadius:999, color: tick.signal==='BUY'?'#a8f06a':tick.signal==='SELL'?'#ff6b6b':'#bfefff'}}>{tick.signal} · 下一根开盘≈{tick.next_open_estimate.toFixed(4)}</span>
          <span style={{background:'#132a2e',border:'1px solid #1c4a52',padding:'4px 8px',borderRadius:999}}>{tick.next_bar_execution}</span>
        </div>
      </div>
      <div style={{padding:'14px 14px', display:'grid', gridTemplateColumns:'1fr 1fr', gap:10, background:'#0a171a'}}>
        <div style={{background:'#101c1f',border:'1px solid #1a3a3e',borderRadius:12,padding:'10px 12px'}}><div style={{font:'11px ui-monospace',color:'#8ea096'}}>账户权益</div><div style={{fontSize:18,fontWeight:800}}>{new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY'}).format(tick.account.equity)}</div><div style={{font:'11px ui-monospace',color:'#8ea096'}}>现金 ¥{tick.account.cash.toLocaleString()} · 持仓 ¥{tick.account.portfolio_value.toLocaleString()}</div></div>
        <div style={{background:'#101c1f',border:'1px solid #1a3a3e',borderRadius:12,padding:'10px 12px'}}><div style={{font:'11px ui-monospace',color:'#8ea096'}}>当日表现</div><div style={{fontSize:18,fontWeight:800, color: tick.risk.daily_drawdown_pct>0 ? '#ff6b6b' : '#a8f06a'}}>{tick.risk.daily_drawdown_pct>0 ? `-${tick.risk.daily_drawdown_pct.toFixed(2)}% 回撤` : `+${(tick.account.equity/tick.risk.initial_equity*100-100).toFixed(2)}%`}</div><div style={{font:'11px ui-monospace',color:'#8ea096'}}>熔断线 {tick.risk.daily_loss_limit_pct.toFixed(2)}% · {tick.risk.can_trade ? '可交易' : '已止买'}</div></div>
        <div style={{background:'#101c1f',border:'1px solid #1a3a3e',borderRadius:12,padding:'10px 12px'}}><div style={{font:'11px ui-monospace',color:'#8ea096'}}>持仓（模拟）</div><div style={{fontSize:14,fontWeight:700}}>{tick.positions.map(p=>`${p.symbol} ${p.qty}股 @${p.avg_entry_price.toFixed(3)}`).join(' · ') || '空仓'}</div><div style={{font:'11px ui-monospace',color:'#8ea096'}}>未实现 {tick.positions.reduce((a,p)=>a+p.unrealized_pl,0).toFixed(2)} CNY</div></div>
        <div style={{background:'#101c1f',border:'1px solid #1a3a3e',borderRadius:12,padding:'10px 12px'}}><div style={{font:'11px ui-monospace',color:'#8ea096'}}>成本（占位）</div><div style={{fontSize:14,fontWeight:700}}>{tick.cost.slippage_bps} bps · {tick.cost.commission_per_order} /笔</div><div style={{font:'11px ui-monospace',color:'#e3a857'}}>{tick.cost.note}</div></div>
        <div style={{gridColumn:'1 / -1', background:'#102024', border:'1px solid #1a3a3e', borderRadius:10, padding:'8px 10px', display:'flex', alignItems:'center', justifyContent:'space-between'}}>
          <div style={{font:'11px ui-monospace', color:'#8ea096', display:'flex', alignItems:'center', gap:6}}><span className="status-dot" style={{background: tick.engine.enabled ? '#a8f06a' : '#e3a857'}}/> 引擎：{tick.engine.note} · {tick.engine.enabled ? 'ARMED' : 'PAUSED'}</div>
          <span style={{font:'11px ui-monospace', color:'#8ea096', background:'#0f262a', padding:'4px 8px', borderRadius:999, border:'1px solid #1a3a3e'}}>仅纸面 · 允许隔夜 · {tick.currency}</span>
        </div>
      </div>
    </div>
  </div>;
}

function SZ159502Page({data,loading,onRefresh,csrf}:{data:SZDashboard|null;loading:boolean;onRefresh:()=>void;csrf:string}){
  if (!data) return <section className="panel full-panel"><div className="panel-header"><div><div className="panel-kicker"><span className="kicker-dot blue"/> 159502 PAPER SIM <span className="panel-separator">/</span>离线纸面</div><h2>159502 嘉实标普生物科技精选行业 ETF（QDII）</h2></div></div><div className="blank-state"><RefreshCw size={22} className={loading?'spin':''}/><strong>离线 Demo 加载中</strong><span>未获授权的五分钟历史不做真实回测；当前为合成演示数据。</span></div></section>;
  const dailyReturn = data.account.initial_equity ? (data.account.equity / data.account.initial_equity - 1) * 100 : 0;
  const ddThreshold = data.risk.daily_loss_limit_fraction * 100;
  const ddCurrent = dailyReturn < 0 ? Math.abs(dailyReturn) : 0;
  const riskProgress = Math.min(ddCurrent / ddThreshold * 100, 100);
  const costNote = data.cost.note;
  const handleEngine = async (action: 'start'|'stop') => {
    if (!csrf) return;
    if (action==='start' && !window.confirm('159502 实时轮询默认关闭，确认已核验授权、额度与风控后再启用？当前为离线 Demo，不会连接实盘。')) return;
    try {
      const res = await fetch(`/api/sz159502/engine/${action}`, { method:'POST', credentials:'same-origin', headers:{'X-CSRF-Token': csrf, 'Content-Type': 'application/json'}, body: action==='start' ? JSON.stringify({acknowledge_risk:false}) : undefined });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || '操作失败');
      alert(action==='start' ? '159502 纸面策略已尝试启用（需先开启轮询与授权）' : '已暂停');
      onRefresh();
    } catch (e) { alert(e instanceof Error ? e.message : '请求失败'); }
  };
  return <>
    <SZLiveTicker />
    <section className="notice-strip" style={{background:'#0f2a2f', borderColor:'#1c4a52', marginTop:10}}><div className="notice-mark"><Wallet size={15}/></div><span><strong>离线/Demo · {data.symbol}</strong><i>—</i>初始 ¥{data.account.initial_equity.toLocaleString()} · 单笔≤{data.risk.per_trade_fraction*100}% · 日熔断 {ddThreshold}% 后止新买并尝试卖出</span><button onClick={onRefresh}><RefreshCw size={13}/> 刷新离线数据</button></section>
    <section className="metric-grid">
      <MetricCard label="159502 账户权益（CNY）" value={money(data.account.equity, 'CNY')} sub={`${data.disclaimer} · ${data.feed}`} icon={Wallet} badge="CNY · PAPER SIM"/>
      <MetricCard label="账户日变动" value={pct(dailyReturn)} sub={`相对初始权益 ¥${data.account.initial_equity.toLocaleString()}`} icon={dailyReturn>=0?TrendingUp:TrendingDown} positive={dailyReturn>=0} badge="DAILY CHANGE"/>
      <MetricCard label="成本假设（占位）" value={`${data.cost.slippage_bps} bps`} sub={costNote} icon={Gauge} badge="PLACEHOLDER"/>
      <MetricCard label="引擎状态" value={data.engine.enabled?'运行中':'已暂停'} sub={data.engine.note} icon={data.engine.enabled?Play:Pause} badge={data.engine.paused_reason || 'DEMO'}/>
    </section>
    <section className="dashboard-grid">
      <div className="panel chart-panel">
        <div className="panel-header chart-header"><div><div className="panel-kicker"><span className="kicker-dot green"/> EQUITY CURVE <span className="panel-separator">/</span>159502 纸面权益</div><h2>纸面轨迹<span className="chart-period-label">· 深圳 09:30-11:30 / 13:00-15:00 · 5m</span></h2></div><div className="chart-actions"><button className="icon-button refresh-button" onClick={onRefresh}><RefreshCw size={15} className={loading?'spin':''}/></button></div></div>
        <div className="chart-summary"><strong>{money(data.account.equity, 'CNY')}</strong><span className={`chart-change ${dailyReturn>=0?'positive':'negative'}`}>{dailyReturn>=0?<ArrowUpRight size={14}/>:<ArrowDownRight size={14}/>} {pct(dailyReturn)} <i>纸面日变动</i></span><span className="chart-updated">离线 Demo · {new Date(data.updated_at).toLocaleTimeString('zh-CN')}</span></div>
        <div className="chart-wrap"><ResponsiveContainer width="100%" height="100%"><AreaChart data={data.equity_curve} margin={{top:18,right:8,left:-17,bottom:0}}><defs><linearGradient id="szFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#7cc4ff" stopOpacity={.22}/><stop offset="96%" stopColor="#7cc4ff" stopOpacity={0}/></linearGradient></defs><CartesianGrid stroke="#ffffff0b" vertical={false}/><XAxis dataKey="time" axisLine={false} tickLine={false} tick={{fill:'#657170',fontSize:10}} interval="preserveStartEnd" minTickGap={38}/><YAxis axisLine={false} tickLine={false} tick={{fill:'#657170',fontSize:10}} tickFormatter={(v:number)=>`¥${Math.round(v/1000)}k`} domain={['dataMin - 500','dataMax + 700']}/><Tooltip content={<ChartTooltip/>}/><Area type="monotone" dataKey="equity" stroke="#7cc4ff" strokeWidth={2} fill="url(#szFill)" activeDot={{r:4, fill:'#d5ffae'}}/><Line type="monotone" dataKey="benchmark" stroke="#7581e5" strokeWidth={1.4} strokeDasharray="4 5" dot={false}/></AreaChart></ResponsiveContainer></div>
        <div className="chart-legend"><span><i className="legend-line legend-equity" style={{background:'#7cc4ff'}}/>纸面权益</span><span><i className="legend-line legend-benchmark"/>Buy & Hold 基准</span><span className="legend-footnote">{costNote} · 不代表真实费率</span></div>
      </div>
      <div className="panel risk-panel">
        <div className="panel-header"><div><div className="panel-kicker"><span className="kicker-dot amber"/> RISK GUARD <span className="panel-separator">/</span>159502 风控</div><h2>护栏状态</h2></div><ShieldCheck size={18} className="panel-header-icon"/></div>
        <div className="risk-status-row"><span className="risk-check"><Check size={14}/></span><div><strong>{data.engine.enabled?'策略运行中': data.engine.paused_reason==='daily_loss_limit' ? '熔断暂停' : '演示/暂停'}</strong><span>{data.engine.note}</span></div><span className="risk-state-tag">{data.engine.enabled?'ARMED':'PAUSED'}</span></div>
        <div className="risk-meter-label"><span>当日回撤熔断</span><strong>{ddThreshold.toFixed(2)}%</strong></div><div className="risk-meter"><span style={{width: `${riskProgress}%`, background: riskProgress>=80 ? '#e35d5d' : '#a8f06a'}}/></div><div className="risk-meter-scale"><span>当前回落 {ddCurrent.toFixed(2)}%</span><span>暂停阈值</span></div>
        <div className="risk-rule-list"><div><span className="rule-icon"><Layers3 size={14}/></span><span>单笔仓位上限（权益）</span><strong>{data.risk.per_trade_fraction*100}%</strong></div><div><span className="rule-icon"><Wallet size={14}/></span><span>初始虚拟资金</span><strong>¥{data.risk.initial_equity.toLocaleString()}</strong></div><div><span className="rule-icon"><LockKeyhole size={14}/></span><span>交易</span><strong>仅纸面 · 允许隔夜</strong></div></div>
        <div className="risk-footer"><CircleHelp size={13}/><span>{data.risk.behavior}；触及后需人工确认方可恢复。</span></div>
        <div style={{display:'flex', gap:8, marginTop:12}}><button className="button button-primary" onClick={()=>handleEngine(data.engine.enabled?'stop':'start')}>{data.engine.enabled?<Pause size={14}/>:<Play size={14}/>} {data.engine.enabled?'暂停策略':'纸面策略（需授权）'}</button><button className="button button-quiet" onClick={onRefresh}>刷新</button></div>
      </div>
    </section>
    <section className="bottom-grid">
      <div className="panel positions-panel"><div className="panel-header table-heading"><div><div className="panel-kicker"><span className="kicker-dot blue"/> OPEN POSITIONS <span className="panel-separator">/</span>当前纸面持仓</div><h2>组合构成 <span className="count-chip">{data.positions.length}</span></h2></div></div><div className="table-scroll"><table><thead><tr><th>标的</th><th>数量</th><th>平均成本</th><th>最新参考</th><th>市值</th><th>未实现盈亏</th></tr></thead><tbody>{data.positions.map(p=><tr key={p.symbol}><td><div className="ticker-cell"><span className="ticker-mark">{p.symbol.slice(0,1)}</span><div><strong>{p.symbol}</strong><small>{data.name}</small></div></div></td><td className="mono">{p.qty}</td><td className="mono">{precise(p.avg_entry_price, 'CNY')}</td><td className="mono">{precise(p.current_price, 'CNY')}</td><td className="mono">{money(p.market_value, 'CNY')}</td><td><span className={`pnl ${p.unrealized_pl>=0?'positive':'negative'}`}>{p.unrealized_pl>=0?'+':''}{precise(p.unrealized_pl, 'CNY')} <small>{pct(p.unrealized_plpc*100)}</small></span></td></tr>)}{data.positions.length===0&&<tr><td colSpan={6} className="empty-cell">没有匹配的持仓</td></tr>}</tbody></table></div><div className="panel-bottom-link"><span>执行：下一根可交易 K 开盘成交 · 成本：{costNote}</span><span>离线/Demo · {data.feed}</span></div></div>
      <div className="panel signals-panel"><div className="panel-header"><div><div className="panel-kicker"><span className="kicker-dot orange"/> SIGNAL FEED <span className="panel-separator">/</span>未验证事项</div><h2>接入状态</h2></div></div><div style={{padding:'12px 16px', display:'grid', gap:8}}>{data.unverified.map((u,i)=><div key={i} style={{display:'flex', gap:8, fontSize:13, color:'#aab6ae'}}><span style={{color:'#e3a857'}}>•</span><span>{u}</span></div>)}<div style={{marginTop:8, padding:10, background:'#102024', borderRadius:8, fontSize:12, color:'#8ea096'}}><strong>同花顺 API Playground 已手动查询：</strong>/api/fund/market/snapshot?thscode=159502.SZ 返回 code=0；但费用、额度、授权范围尚未确认，未发起额外真实调用，实时轮询默认关闭。以服务端环境变量 TONGHUASHUN_API_KEY 为唯一可信来源。</div><div style={{fontSize:12, color:'#8ea096'}}>历史基金接口为日线；高频分钟接口不应被假装为可用。无已授权 159502 五分钟历史时，不做真实数据回测。</div></div></div>
    </section>
  </>;
}

function SettingLine({title,sub,value}:{title:string;sub:string;value:string}){return <div className="setting-line"><div><strong>{title}</strong><small>{sub}</small></div><span className="setting-value">{value}</span></div>;}
function SecurityNote({icon:Icon,title,text}:{icon:typeof LockKeyhole;title:string;text:string}){return <div className="security-note"><Icon size={17}/><div><strong>{title}</strong><span>{text}</span></div></div>;}
