import streamlit as st
import pandas as pd
import io

# ==========================================
# 1. 基础配置
# ==========================================
st.set_page_config(page_title="智能调拨系统 V32.0 (供需预判版)", layout="wide", page_icon="🦁")

hide_st_style = """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    [data-testid="stToolbar"] {visibility: hidden !important; display: none !important; height: 0px !important;}
    .block-container {padding-top: 1rem !important;}
    </style>
    """
st.markdown(hide_st_style, unsafe_allow_html=True)
st.title("🦁 智能库存分配 V32.0 (供需预判 + 调拨指引)")

# ==========================================
# 2. 数据清洗与辅助函数
# ==========================================
def clean_number(x):
    if pd.isna(x): return 0
    s = str(x).strip().replace(',', '').replace(' ', '')
    try: return float(s)
    except: return 0

def to_int(x):
    try: return int(round(float(x)))
    except: return 0

def normalize_str(s):
    if pd.isna(s): return ""
    return str(s).strip().upper()

def normalize_wh_name(name):
    n = normalize_str(name)
    if "深" in n: return "深仓"
    if "外协" in n: return "外协"
    if "云" in n or "天源" in n: return "云仓"
    return "其他" # PO和计划单独处理

def load_and_find_header(file):
    if not file: return None, "未上传"
    try:
        file.seek(0)
        if file.name.endswith('.csv'):
            try: df = pd.read_csv(file, encoding='utf-8-sig')
            except: 
                file.seek(0)
                df = pd.read_csv(file, encoding='gbk')
        else:
            df = pd.read_excel(file)
            
        header_idx = -1
        for i, row in df.head(30).iterrows():
            row_str = " ".join([str(v).upper() for v in row.values])
            if "SKU" in row_str:
                header_idx = i
                break
        
        if header_idx != -1:
            df.columns = df.iloc[header_idx]
            df = df.iloc[header_idx+1:]
        
        df.reset_index(drop=True, inplace=True)
        df.columns = [str(c).strip() for c in df.columns]
        df.dropna(how='all', inplace=True)
        return df, None
    except Exception as e:
        return None, f"读取错误: {str(e)}"

# ==========================================
# 3. 核心：库存管理器
# ==========================================
class InventoryManager:
    def __init__(self, df_inv, df_po, df_plan):
        # stock[sku][fnsku][wh_type] = List[Dict]
        self.stock = {} 
        # inbound[sku][fnsku] = List[Dict] (包含 PO 和 提货计划)
        self.inbound = {} 
        self.cleaning_logs = []
        
        self._init_inventory(df_inv)
        self._init_inbound(df_po, '采购订单')
        self._init_inbound(df_plan, '提货计划') # 提货计划视为供应

    def _init_inventory(self, df):
        if df is None or df.empty: return
        
        c_sku = next((c for c in df.columns if 'SKU' in c.upper()), None)
        c_fnsku = next((c for c in df.columns if 'FNSKU' in c.upper()), None)
        c_wh = next((c for c in df.columns if '仓库' in c), None)
        c_zone = next((c for c in df.columns if '库区' in c), None)
        if not c_zone:
            c_zone = next((c for c in df.columns if any(k in c.upper() for k in ['库位', 'ZONE', 'LOCATION'])), None)
        c_qty = next((c for c in df.columns if '可用' in c), None)
        if not c_qty:
            c_qty = next((c for c in df.columns if '数量' in c or '库存' in c), None)

        if not (c_sku and c_wh and c_qty): 
            self.cleaning_logs.append({"类型": "错误", "原因": "库存表缺少关键列"})
            return

        for idx, row in df.iterrows():
            w_name_raw = str(row.get(c_wh, ''))
            w_name_norm = normalize_str(w_name_raw)
            sku = str(row.get(c_sku, '')).strip()
            
            if any(k in w_name_norm for k in ["沃尔玛", "WALMART", "TEMU"]):
                self.cleaning_logs.append({"类型": "库存过滤", "SKU": sku, "原因": f"黑名单仓库 ({w_name_raw})"})
                continue
            
            if not sku: continue
            
            f_raw = row.get(c_fnsku, '')
            fnsku = str(f_raw).strip() if pd.notna(f_raw) else ""
            qty = clean_number(row.get(c_qty, 0))
            zone = str(row.get(c_zone, '')).strip() if c_zone else "-"
            
            if qty <= 0: continue
            
            w_type = normalize_wh_name(w_name_raw)
            
            if sku not in self.stock: self.stock[sku] = {}
            if fnsku not in self.stock[sku]: 
                self.stock[sku][fnsku] = {'深仓':[], '外协':[], '云仓':[], '采购订单':[], '其他':[]}
            
            self.stock[sku][fnsku][w_type].append({
                'qty': qty, 'raw_name': w_name_raw, 'zone': zone
            })

    def _init_inbound(self, df, source_type):
        if df is None or df.empty: return
        
        c_sku = next((c for c in df.columns if 'SKU' in c.upper()), None)
        c_fnsku = next((c for c in df.columns if 'FNSKU' in c.upper()), None)
        
        # 兼容 PO 和 Plan 的列名
        c_qty = next((c for c in df.columns if '未入库' in c), None)
        if not c_qty: c_qty = next((c for c in df.columns if '数量' in c), None)
        
        # 仅 PO 有黑名单检查
        c_req = next((c for c in df.columns if '人' in c or '员' in c), None)
        block_list = ["陈丹丹", "张萍", "杨上儒", "陈炜填", "贝少婷", "詹翠萍"]
        
        for idx, row in df.iterrows():
            sku = str(row.get(c_sku, '')).strip()
            
            if source_type == '采购订单' and c_req:
                req = str(row.get(c_req, ''))
                if any(b in req for b in block_list):
                    self.cleaning_logs.append({"类型": f"{source_type}过滤", "SKU": sku, "原因": f"黑名单人员 ({req})"})
                    continue
            
            qty = clean_number(row.get(c_qty, 0))
            f_raw = row.get(c_fnsku, '') if c_fnsku else ''
            fnsku = str(f_raw).strip() if pd.notna(f_raw) else ""
            
            if sku and qty > 0:
                if sku not in self.inbound: self.inbound[sku] = {}
                if fnsku not in self.inbound[sku]: self.inbound[sku][fnsku] = []
                
                self.inbound[sku][fnsku].append({
                    'qty': qty, 'raw_name': source_type, 'zone': '-'
                })

    def get_total_supply(self, sku):
        """计算该 SKU 的总供应量 (Stock + PO + Plan)"""
        total = 0
        if sku in self.stock:
            for f in self.stock[sku]:
                for w in self.stock[sku][f]:
                    total += sum(i['qty'] for i in self.stock[sku][f][w])
        if sku in self.inbound:
            for f in self.inbound[sku]:
                total += sum(i['qty'] for i in self.inbound[sku][f])
        return total

    def get_snapshot(self, sku):
        res = {'深仓':0, '外协':0, '云仓':0, '采购订单': 0, '提货计划': 0}
        if sku in self.stock:
            for f in self.stock[sku]:
                for w_type in ['深仓', '外协', '云仓']:
                    res[w_type] += sum(item['qty'] for item in self.stock[sku][f].get(w_type, []))
        if sku in self.inbound:
            for f in self.inbound[sku]:
                for item in self.inbound[sku][f]:
                    if item['raw_name'] == '采购订单': res['采购订单'] += item['qty']
                    elif item['raw_name'] == '提货计划': res['提货计划'] += item['qty']
        return res

    def execute_deduction(self, sku, target_fnsku, qty_needed, strategy_chain, mode='strict_only'):
        qty_remain = qty_needed
        breakdown_notes = []
        process_details = {'raw_wh': [], 'zone': [], 'fnsku': [], 'qty': 0}
        deduction_log = []
        usage_breakdown = {}
        
        for src_type, src_name in strategy_chain:
            if qty_remain <= 0: break
            step_taken = 0
            
            # --- STOCK 处理 ---
            if src_type == 'stock' and sku in self.stock:
                # A. 严格匹配
                if mode in ['mixed', 'strict_only']:
                    if target_fnsku in self.stock[sku]:
                        items = self.stock[sku][target_fnsku].get(src_name, [])
                        for item in items:
                            if qty_remain <= 0: break
                            avail = item['qty']
                            if avail <= 0: continue
                            take = min(avail, qty_remain)
                            item['qty'] -= take
                            qty_remain -= take
                            step_taken += take
                            deduction_log.append(f"{src_name}(直发,-{to_int(take)})")
                
                # B. 加工匹配
                if mode in ['mixed', 'process_only'] and (qty_remain > 0 or mode == 'process_only'):
                    if qty_remain > 0:
                        for other_f in self.stock[sku]:
                            if other_f == target_fnsku: continue
                            if qty_remain <= 0: break
                            items = self.stock[sku][other_f].get(src_name, [])
                            for item in items:
                                if qty_remain <= 0: break
                                avail = item['qty']
                                if avail <= 0: continue
                                take = min(avail, qty_remain)
                                item['qty'] -= take
                                qty_remain -= take
                                step_taken += take
                                breakdown_notes.append(f"{src_name}(加工)")
                                process_details['raw_wh'].append(item['raw_name'])
                                process_details['zone'].append(item['zone'])
                                process_details['fnsku'].append(other_f)
                                process_details['qty'] += take
                                deduction_log.append(f"{src_name}(加工,-{to_int(take)})")

            # --- INBOUND (PO/Plan) 处理 ---
            elif src_type == 'inbound' and sku in self.inbound:
                # 筛选特定的 source (PO 或 提货计划)
                # 注：inbound[sku][fnsku] 列表里混合了 PO 和 Plan
                
                # A. 任意模式 / 严格模式
                if mode in ['inbound_any', 'strict_only']:
                    targets = []
                    if mode == 'strict_only':
                        if target_fnsku in self.inbound[sku]: targets = [target_fnsku]
                    else:
                        targets = list(self.inbound[sku].keys())
                        
                    for f in targets:
                        if qty_remain <= 0: break
                        items = self.inbound[sku][f]
                        for item in items:
                            if item['raw_name'] != src_name: continue # 过滤：只扣当前指定的源（如'采购订单'）
                            if qty_remain <= 0: break
                            avail = item['qty']
                            if avail <= 0: continue
                            take = min(avail, qty_remain)
                            item['qty'] -= take
                            qty_remain -= take
                            step_taken += take
                            tag = f"{src_name}精准" if mode == 'strict_only' else f"{src_name}任意"
                            deduction_log.append(f"{tag}(-{to_int(take)})")

                # B. 加工模式
                if mode == 'process_only' and qty_remain > 0:
                    for other_f in self.inbound[sku]:
                        if other_f == target_fnsku: continue
                        if qty_remain <= 0: break
                        items = self.inbound[sku][other_f]
                        for item in items:
                            if item['raw_name'] != src_name: continue
                            if qty_remain <= 0: break
                            avail = item['qty']
                            if avail <= 0: continue
                            take = min(avail, qty_remain)
                            item['qty'] -= take
                            qty_remain -= take
                            step_taken += take
                            breakdown_notes.append(f"{src_name}(加工)")
                            process_details['raw_wh'].append(src_name)
                            process_details['zone'].append('-')
                            process_details['fnsku'].append(other_f)
                            process_details['qty'] += take
                            deduction_log.append(f"{src_name}加工(-{to_int(take)})")
            
            if step_taken > 0:
                usage_breakdown[src_name] = usage_breakdown.get(src_name, 0) + step_taken
                used_sources.append(src_name)

        return qty_remain, usage_breakdown, process_details, deduction_log

# ==========================================
# 4. 主逻辑流程
# ==========================================
def run_allocation(df_input, inv_mgr, mapping):
    
    # === 1. 全局供需预判 (Step 0) ===
    # 计算每个 SKU 的总需求和总供给
    col_sku = mapping['SKU']
    col_qty = mapping['数量']
    
    demand_summary = df_input.groupby(col_sku)[col_qty].apply(lambda x: sum(clean_number(v) for v in x)).to_dict()
    order_list = []
    
    for sku, req_qty in demand_summary.items():
        sku = str(sku).strip()
        total_supply = inv_mgr.get_total_supply(sku)
        gap = req_qty - total_supply
        if gap > 0:
            order_list.append({
                "SKU": sku,
                "总需求": to_int(req_qty),
                "现有供应(库+PO+计)": to_int(total_supply),
                "建议下单数量": to_int(gap)
            })
    
    df_order_advice = pd.DataFrame(order_list)

    # === 2. 任务拆解与优先级 ===
    tiers = {1: [], 2: []} # Tier 1: Non-US, Tier 2: US
    calc_logs = []
    
    col_tag = mapping['标签']
    col_country = mapping['国家']
    col_fnsku = mapping['FNSKU']
    
    for idx, row in df_input.iterrows():
        tag = str(row.get(col_tag, '')).strip()
        country = str(row.get(col_country, '')).strip()
        sku = str(row.get(col_sku, '')).strip()
        fnsku = str(row.get(col_fnsku, '')).strip()
        qty = clean_number(row.get(col_qty, 0))
        
        if qty <= 0 or not sku: continue
        
        is_us = 'US' in country.upper() or '美国' in country
        # 优先级只看 US vs Non-US
        priority = 2 if is_us else 1
            
        task = {
            'row_idx': idx, 'priority': priority,
            'sku': sku, 'fnsku': fnsku, 'qty': qty, 'country': country,
            'is_us': is_us, 'tag': tag,
            'filled': 0, 'usage': {}, 
            'proc': {'raw_wh': [], 'zone': [], 'fnsku': [], 'qty': 0},
            'logs': []
        }
        tiers[priority].append(task)

    results_map = {}
    
    # 策略定义
    # US: 外 > 云 > PO/Plan > 深
    strat_us = [('stock', '外协'), ('stock', '云仓'), ('inbound', '提货计划'), ('inbound', '采购订单'), ('stock', '深仓')] 
    # Non-US: 深 > 外 > 云 > PO/Plan
    strat_non_us = [('stock', '深仓'), ('stock', '外协'), ('stock', '云仓'), ('inbound', '提货计划'), ('inbound', '采购订单')]
    
    def update_task(t, rem, usage, proc, logs):
        step_fill = (t['qty'] - t['filled']) - rem
        t['filled'] += step_fill
        for k, v in usage.items(): t['usage'][k] = t['usage'].get(k, 0) + v
        if logs: t['logs'].extend(logs)
        if proc:
            t['proc']['raw_wh'].extend(proc['raw_wh'])
            t['proc']['zone'].extend(proc['zone'])
            t['proc']['fnsku'].extend(proc['fnsku'])
            t['proc']['qty'] += proc['qty']

    # === 3. 梯队计算 ===
    
    # Tier 1: 非 US (Non-US)
    current_tasks = tiers[1]
    if current_tasks:
        # R1: 现货精准 (Global Strict Stock)
        # 策略里包含了 stock 和 inbound，我们需要手动拆解一下只跑 stock strict
        # 简化：直接用完整 strat 跑 strict_only，但只包含 stock 类型
        strat_stock_only = [x for x in strat_non_us if x[0] == 'stock']
        for t in current_tasks:
            rem = t['qty'] - t['filled']
            if rem <= 0: continue
            rem, u, p_, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_stock_only, 'strict_only')
            update_task(t, rem, u, p_, [f"[R1现货精准]:{x}" for x in l])
            
        # R2: 现货加工 (Global Process Stock)
        for t in current_tasks:
            rem = t['qty'] - t['filled']
            if rem <= 0: continue
            rem, u, p_, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_stock_only, 'process_only')
            update_task(t, rem, u, p_, [f"[R2现货加工]:{x}" for x in l])
            
        # R3: PO/Plan 盲配 (Global Inbound Any)
        strat_inbound_only = [x for x in strat_non_us if x[0] == 'inbound']
        for t in current_tasks:
            rem = t['qty'] - t['filled']
            if rem <= 0: continue
            rem, u, p_, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_inbound_only, 'inbound_any')
            update_task(t, rem, u, p_, [f"[R3供应盲配]:{x}" for x in l])
            
        for t in current_tasks:
            if t['filled'] < t['qty']: t['logs'].append(f"缺口 {to_int(t['qty'] - t['filled'])}")
            results_map[t['row_idx']] = t
            calc_logs.append({
                "步骤": "Tier 1 (Non-US)", "SKU": t['sku'], "FNSKU": t['fnsku'], 
                "执行过程": " || ".join(t['logs']), "最终发货": to_int(t['filled'])
            })

    # Tier 2: US
    current_tasks = tiers[2]
    if current_tasks:
        strat_stock_only = [x for x in strat_us if x[0] == 'stock']
        strat_inbound_only = [x for x in strat_us if x[0] == 'inbound']
        
        # R1: 现货精准
        for t in current_tasks:
            rem = t['qty'] - t['filled']
            if rem <= 0: continue
            rem, u, p_, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_stock_only, 'strict_only')
            update_task(t, rem, u, p_, [f"[R1现货精准]:{x}" for x in l])
            
        # R2: PO/Plan 精准
        for t in current_tasks:
            rem = t['qty'] - t['filled']
            if rem <= 0: continue
            rem, u, p_, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_inbound_only, 'strict_only')
            update_task(t, rem, u, p_, [f"[R2供应精准]:{x}" for x in l])
            
        # R3: 现货加工
        for t in current_tasks:
            rem = t['qty'] - t['filled']
            if rem <= 0: continue
            rem, u, p_, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_stock_only, 'process_only')
            update_task(t, rem, u, p_, [f"[R3现货加工]:{x}" for x in l])
            
        # R4: PO/Plan 加工
        for t in current_tasks:
            rem = t['qty'] - t['filled']
            if rem <= 0: continue
            rem, u, p_, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_inbound_only, 'process_only')
            update_task(t, rem, u, p_, [f"[R4供应加工]:{x}" for x in l])

        for t in current_tasks:
            if t['filled'] < t['qty']: t['logs'].append(f"缺口 {to_int(t['qty'] - t['filled'])}")
            results_map[t['row_idx']] = t
            calc_logs.append({
                "步骤": "Tier 2 (US)", "SKU": t['sku'], "FNSKU": t['fnsku'], 
                "执行过程": " || ".join(t['logs']), "最终发货": to_int(t['filled'])
            })

    # --- 4. 构建输出 ---
    output_rows = []
    display_order = ['深仓', '外协', '云仓', '提货计划', '采购订单']
    display_map = {'深仓':'深仓库存', '外协':'外协仓库存', '云仓':'云仓库存', '提货计划':'提货计划', '采购订单':'采购订单'}
    
    sku_shortage_map = {} 
    for idx, row in df_input.iterrows():
        t = results_map.get(idx)
        if t:
            gap = t['qty'] - t['filled']
            if gap > 0.001: sku_shortage_map[t['sku']] = sku_shortage_map.get(t['sku'], 0) + gap
            
    for idx, row in df_input.iterrows():
        t = results_map.get(idx)
        out_row = row.to_dict()
        if t:
            status_parts = []
            for k in display_order:
                val = t['usage'].get(k, 0)
                if val > 0: 
                    s_text = f"{display_map[k]}{to_int(val)}"
                    # 非US 使用外协 标记
                    if not t['is_us'] and k == '外协':
                        s_text += "(需调回深仓)"
                    status_parts.append(s_text)
            
            status_str = "+".join(status_parts)
            if t['filled'] < t['qty']: status_str += f"+待下单(缺{to_int(t['qty'] - t['filled'])})"
            if not status_str: status_str = "待下单"
            
            p_wh = "; ".join(list(set(t['proc']['raw_wh'])))
            p_zone = "; ".join(list(set(t['proc']['zone'])))
            p_fn = "; ".join(list(set(t['proc']['fnsku'])))
            p_qt = to_int(t['proc']['qty']) if t['proc']['qty'] > 0 else ""
            
            snap = inv_mgr.get_snapshot(t['sku'])
            total_short = sku_shortage_map.get(t['sku'], 0)
            short_stat = f"❌ 缺货 (该SKU总缺 {to_int(total_short)})" if total_short > 0 else "✅ 全满足"
            
            out_row.update({
                "库存状态": status_str,
                "最终发货数量": to_int(t['filled']),
                "缺货与否": short_stat,
                "加工库区": p_wh, "加工库区_库位": p_zone, "加工FNSKU": p_fn, "加工数量": p_qt,
                "剩_深仓": to_int(snap['深仓']), "剩_外协": to_int(snap['外协']),
                "剩_云仓": to_int(snap['云仓']), "剩_PO": to_int(snap['采购订单']), "剩_计划": to_int(snap['提货计划'])
            })
        else:
             out_row.update({"库存状态": "-", "最终发货数量": 0, "缺货与否": "-"})
        output_rows.append(out_row)

    return pd.DataFrame(output_rows), calc_logs, inv_mgr.cleaning_logs, df_order_advice

# ==========================================
# 5. UI 渲染
# ==========================================
if 'df_demand' not in st.session_state:
    st.session_state.df_demand = pd.DataFrame(columns=["标签", "国家", "SKU", "FNSKU", "数量", "运营", "店铺", "备注"])

col_main, col_side = st.columns([75, 25])

with col_main:
    st.subheader("1. 需求填报 (V32.0)")
    edited_df = st.data_editor(st.session_state.df_demand, num_rows="dynamic", use_container_width=True, height=400)
    
    cols = list(edited_df.columns)
    def get_idx(cands):
        for i, c in enumerate(cols):
            if c in cands: return i
        return 0

    st.write("🔧 **列映射配置**")
    c1, c2, c3, c4, c5 = st.columns(5)
    map_tag = c1.selectbox("标签列", cols, index=get_idx(['标签']))
    map_country = c2.selectbox("国家列", cols, index=get_idx(['国家']))
    map_sku = c3.selectbox("SKU列", cols, index=get_idx(['SKU']))
    map_fnsku = c4.selectbox("FNSKU列", cols, index=get_idx(['FNSKU']))
    map_qty = c5.selectbox("数量列", cols, index=get_idx(['数量']))
    mapping = {'标签': map_tag, '国家': map_country, 'SKU': map_sku, 'FNSKU': map_fnsku, '数量': map_qty}

with col_side:
    st.subheader("2. 库存文件")
    f_inv = st.file_uploader("库存表 (必含'可用')", type=['xlsx', 'xls', 'csv'])
    f_po = st.file_uploader("PO表 (必含'未入库')", type=['xlsx', 'xls', 'csv'])
    f_plan = st.file_uploader("提货计划表 (作供应源)", type=['xlsx', 'xls', 'csv'])
    
    if st.button("🚀 开始计算", type="primary", use_container_width=True):
        if f_inv and f_po and not edited_df.empty:
            with st.spinner("执行供需预判及分配..."):
                df_inv_raw, err1 = load_and_find_header(f_inv)
                df_po_raw, err2 = load_and_find_header(f_po)
                df_plan_raw, _ = load_and_find_header(f_plan)
                
                if err1: st.error(err1)
                elif err2: st.error(err2)
                else:
                    mgr = InventoryManager(df_inv_raw, df_po_raw, df_plan_raw)
                    final_df, logs, cleans, order_advice = run_allocation(edited_df, mgr, mapping)
                    
                    st.success("计算完成!")
                    
                    if not order_advice.empty:
                        st.error(f"⚠️ 发现 {len(order_advice)} 个SKU存在总缺口，请优先下单！")
                        st.dataframe(order_advice, use_container_width=True)
                    else:
                        st.success("✅ 供需平衡，库存充足")
                    
                    tab1, tab2, tab3 = st.tabs(["📋 分配结果", "🔍 运算日志", "🧹 清洗日志"])
                    
                    with tab1:
                        def highlight(row):
                            if "缺货" in str(row.get('缺货与否', '')): return ['background-color: #ffcdd2'] * len(row)
                            return [''] * len(row)
                        st.dataframe(final_df.style.apply(highlight, axis=1), use_container_width=True)
                    
                    with tab2: st.dataframe(pd.DataFrame(logs), use_container_width=True)
                    with tab3: st.dataframe(pd.DataFrame(cleans), use_container_width=True)
                    
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                        final_df.to_excel(writer, sheet_name='分配结果', index=False)
                        if not order_advice.empty: order_advice.to_excel(writer, sheet_name='待下单清单', index=False)
                        pd.DataFrame(logs).to_excel(writer, sheet_name='运算日志', index=False)
                        pd.DataFrame(cleans).to_excel(writer, sheet_name='清洗日志', index=False)
                    
                    st.download_button("📥 下载完整结果.xlsx", buf.getvalue(), "V32_Result.xlsx")
        else:
            st.warning("请填写需求数据并上传库存文件")
