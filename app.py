import streamlit as st
import pandas as pd
import io
import copy

# ==========================================
# 1. 基础配置
# ==========================================
st.set_page_config(page_title="智能调拨系统 V28.0 (PO精准匹配版)", layout="wide", page_icon="🦁")

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
st.title("🦁 智能库存分配 V28.0 (PO纳入FNSKU精准匹配)")

# ==========================================
# 2. 数据清洗与辅助函数
# ==========================================
def clean_number(x):
    """强制清洗为数字"""
    if pd.isna(x): return 0
    s = str(x).strip().replace(',', '').replace(' ', '')
    try: return float(s)
    except: return 0

def to_int(x):
    """四舍五入转整数"""
    try: return int(round(float(x)))
    except: return 0

def normalize_str(s):
    if pd.isna(s): return ""
    return str(s).strip().upper()

def normalize_wh_name(name):
    """仓库名称标准化"""
    n = normalize_str(name)
    if "深" in n: return "深仓"
    if "外协" in n: return "外协"
    if "云" in n or "天源" in n: return "云仓"
    if "PO" in n or "采购" in n: return "采购订单"
    return "其他"

def load_and_find_header(file, type_tag):
    """读取上传文件"""
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
        # 扩大搜索范围
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
# 3. 核心：库存管理器 (PO结构升级)
# ==========================================
class InventoryManager:
    def __init__(self, df_inv, df_po):
        # 结构: self.stock[sku][fnsku][wh_type] = List[Dict]
        self.stock = {} 
        # 结构升级: self.po[sku][fnsku] = List[Dict] (对齐Stock结构)
        self.po = {} 
        self.cleaning_logs = []
        
        self._init_inventory(df_inv)
        self._init_po(df_po)

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
                'qty': qty,
                'raw_name': w_name_raw,
                'zone': zone
            })

    def _init_po(self, df):
        if df is None or df.empty: return
        
        c_sku = next((c for c in df.columns if 'SKU' in c.upper()), None)
        # 尝试寻找 FNSKU 列
        c_fnsku = next((c for c in df.columns if 'FNSKU' in c.upper()), None)
        
        c_qty = next((c for c in df.columns if '未入库' in c), None)
        if not c_qty: c_qty = next((c for c in df.columns if '数量' in c), None)
        c_req = next((c for c in df.columns if '人' in c or '员' in c), None)
        
        block_list = ["陈丹丹", "张萍", "杨上儒", "陈炜填", "贝少婷", "詹翠萍"]
        
        for idx, row in df.iterrows():
            sku = str(row.get(c_sku, '')).strip()
            if c_req:
                req = str(row.get(c_req, ''))
                if any(b in req for b in block_list):
                    self.cleaning_logs.append({"类型": "PO过滤", "SKU": sku, "原因": f"黑名单人员 ({req})"})
                    continue
            
            qty = clean_number(row.get(c_qty, 0))
            
            # PO 的 FNSKU 处理
            f_raw = row.get(c_fnsku, '') if c_fnsku else ''
            fnsku = str(f_raw).strip() if pd.notna(f_raw) else ""
            
            # 如果没填 FNSKU，虽然不完美，但暂存为空字符，后续作为 Generic 匹配
            
            if sku and qty > 0:
                if sku not in self.po: self.po[sku] = {}
                if fnsku not in self.po[sku]: self.po[sku][fnsku] = []
                
                # PO 统一存储，不分仓库类型，直接作为 '采购订单'
                self.po[sku][fnsku].append({
                    'qty': qty,
                    'raw_name': '采购订单', # 默认名
                    'zone': '-'
                })

    def get_snapshot(self, sku):
        res = {'深仓':0, '外协':0, '云仓':0, '采购订单': 0}
        
        # 统计 Stock
        if sku in self.stock:
            for f in self.stock[sku]:
                for w_type in ['深仓', '外协', '云仓']:
                    res[w_type] += sum(item['qty'] for item in self.stock[sku][f].get(w_type, []))
        
        # 统计 PO (遍历所有 FNSKU)
        if sku in self.po:
            for f in self.po[sku]:
                res['采购订单'] += sum(item['qty'] for item in self.po[sku][f])
                
        return res

    def execute_deduction(self, sku, target_fnsku, qty_needed, strategy_chain, mode='strict_only'):
        """
        核心扣减逻辑 V28 (Stock 和 PO 逻辑统一)
        mode: 
          'strict_only': 仅扣减 Stock/PO 中 FNSKU 完全匹配的
          'process_only': 仅扣减 Stock/PO 中 FNSKU 不匹配的 (加工)
        """
        qty_remain = qty_needed
        breakdown_notes = []
        used_sources = []
        process_details = {'raw_wh': [], 'zone': [], 'fnsku': [], 'qty': 0}
        deduction_log = [] 
        
        for src_type, src_name in strategy_chain:
            if qty_remain <= 0: break
            
            step_taken = 0
            
            # 统一获取数据源
            source_data = None
            if src_type == 'stock':
                source_data = self.stock.get(sku)
            elif src_type == 'po':
                source_data = self.po.get(sku)
            
            if source_data:
                # --- A. 精准匹配 (Strict) ---
                if mode == 'strict_only':
                    if target_fnsku in source_data:
                        # 对于 Stock，要看 src_name (如'深仓')；对于 PO，通常只有默认列表，但为了统一，po结构里我们也只存list
                        # 这里需要区分：Stock有子层级(wh_type)，PO目前设计为直接是list? 
                        # 为了复用，InventoryManager._init_po 中应该保持一致性
                        # 修正: PO 在 init 时没分 wh_type。
                        
                        items = []
                        if src_type == 'stock':
                            items = source_data[target_fnsku].get(src_name, [])
                        else:
                            # PO 不分仓库名，直接取 list
                            items = source_data[target_fnsku] 
                        
                        for item in items:
                            if qty_remain <= 0: break
                            avail = item['qty']
                            if avail <= 0: continue
                            
                            take = min(avail, qty_remain)
                            item['qty'] -= take
                            qty_remain -= take
                            step_taken += take
                            deduction_log.append(f"{src_name}(直发,-{to_int(take)})")
                
                # --- B. 加工 (Process) ---
                elif mode == 'process_only':
                    for other_f in source_data:
                        if other_f == target_fnsku: continue
                        if qty_remain <= 0: break
                        
                        items = []
                        if src_type == 'stock':
                            items = source_data[other_f].get(src_name, [])
                        else:
                            items = source_data[other_f]
                            
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
                            deduction_log.append(f"{src_name}加工(-{to_int(take)})")
            
            if step_taken > 0:
                used_sources.append(src_name)

        return qty_remain, breakdown_notes, used_sources, process_details, deduction_log

# ==========================================
# 4. 主逻辑流程 (四轮扫描)
# ==========================================
def run_allocation(df_input, inv_mgr, df_plan, mapping):
    tasks = []
    calc_logs = []
    
    # --- 1. 提货计划 (Tier -1) ---
    if df_plan is not None and not df_plan.empty:
        c_sku = next((c for c in df_plan.columns if 'SKU' in c.upper()), None)
        c_qty = next((c for c in df_plan.columns if '数量' in c or '计划' in c), None)
        c_country = next((c for c in df_plan.columns if '国家' in c), None)
        c_fnsku = next((c for c in df_plan.columns if 'FNSKU' in c.upper()), None)
        
        if c_sku and c_qty:
            for _, row in df_plan.iterrows():
                sku = str(row.get(c_sku, '')).strip()
                f_raw = row.get(c_fnsku, '') if c_fnsku else ''
                fnsku = str(f_raw).strip() if pd.notna(f_raw) else ""
                qty = clean_number(row.get(c_qty, 0))
                cty = str(row.get(c_country, 'Non-US'))
                if qty > 0:
                    snap = inv_mgr.get_snapshot(sku)
                    # 计划表简单扣减：先现货，后PO
                    strat_stock = [('stock', '深仓'), ('stock', '外协'), ('stock', '云仓')]
                    rem, _, _, _, logs1 = inv_mgr.execute_deduction(sku, fnsku, qty, strat_stock, 'strict_only')
                    rem, _, _, _, logs2 = inv_mgr.execute_deduction(sku, fnsku, rem, [('po', '采购订单')], 'strict_only')
                    
                    calc_logs.append({
                        "步骤": "Tier -1 (计划)", "SKU": sku, "需求": to_int(qty),
                        "库存快照": f"深:{to_int(snap['深仓'])} 外:{to_int(snap['外协'])} PO:{to_int(snap['采购订单'])}",
                        "计算详情": " -> ".join(logs1 + logs2)
                    })

    # --- 2. 任务拆解 ---
    col_tag = mapping['标签']
    col_country = mapping['国家']
    col_sku = mapping['SKU']
    col_fnsku = mapping['FNSKU']
    col_qty = mapping['数量']
    
    for idx, row in df_input.iterrows():
        tag = str(row.get(col_tag, '')).strip()
        country = str(row.get(col_country, '')).strip()
        sku = str(row.get(col_sku, '')).strip()
        fnsku = str(row.get(col_fnsku, '')).strip()
        qty = clean_number(row.get(col_qty, 0))
        
        if qty <= 0 or not sku: continue
        
        is_us = 'US' in country.upper() or '美国' in country
        is_new = '新增' in tag
        
        priority = 0
        if is_new: priority = 2 if is_us else 1
        else: priority = 4 if is_us else 3
            
        tasks.append({
            'row_idx': idx, 'priority': priority,
            'sku': sku, 'fnsku': fnsku, 'qty': qty, 'country': country,
            'is_us': is_us, 'tag': tag,
            'filled': 0, 'notes': [], 'srcs': [], 
            'proc': {'raw_wh': [], 'zone': [], 'fnsku': [], 'qty': 0},
            'logs': []
        })

    tasks.sort(key=lambda x: x['priority'])
    
    # ========================================================
    # 核心算法：四轮扫描法 (V28 PO 精准升级)
    # 1. 现货精准匹配
    # 2. PO 精准匹配
    # 3. 现货加工
    # 4. PO 加工
    # ========================================================
    
    strat_us = [('stock', '外协'), ('stock', '云仓'), ('stock', '深仓')] 
    strat_non_us = [('stock', '深仓'), ('stock', '外协'), ('stock', '云仓')]
    
    # --- Round 1: 现货精准匹配 (Strict Stock) ---
    for t in tasks:
        rem_qty = t['qty'] - t['filled']
        if rem_qty <= 0: continue
        strat = strat_us if t['is_us'] else strat_non_us
        
        rem, _, srcs, _, logs = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem_qty, strat, mode='strict_only')
        
        t['filled'] += (rem_qty - rem)
        t['srcs'].extend(srcs)
        if logs: t['logs'].extend([f"[R1现货]: {l}" for l in logs])

    # --- Round 2: PO 精准匹配 (Strict PO) ---
    for t in tasks:
        rem_qty = t['qty'] - t['filled']
        if rem_qty <= 0: continue
        
        # PO 不分站点策略，统一扣减
        strat = [('po', '采购订单')]
        rem, _, srcs, _, logs = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem_qty, strat, mode='strict_only')
        
        t['filled'] += (rem_qty - rem)
        t['srcs'].extend(srcs)
        if logs: t['logs'].extend([f"[R2采购]: {l}" for l in logs])

    # --- Round 3: 现货加工 (Process Stock) ---
    for t in tasks:
        rem_qty = t['qty'] - t['filled']
        if rem_qty <= 0: continue
        strat = strat_us if t['is_us'] else strat_non_us
        
        rem, _, srcs, proc, logs = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem_qty, strat, mode='process_only')
        
        t['filled'] += (rem_qty - rem)
        t['srcs'].extend(srcs)
        t['proc']['raw_wh'].extend(proc['raw_wh'])
        t['proc']['zone'].extend(proc['zone'])
        t['proc']['fnsku'].extend(proc['fnsku'])
        t['proc']['qty'] += proc['qty']
        if logs: t['logs'].extend([f"[R3加工]: {l}" for l in logs])

    # --- Round 4: PO 加工 (Process PO) ---
    for t in tasks:
        rem_qty = t['qty'] - t['filled']
        if rem_qty <= 0: continue
        
        strat = [('po', '采购订单')]
        rem, _, srcs, proc, logs = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem_qty, strat, mode='process_only')
        
        t['filled'] += (rem_qty - rem)
        t['srcs'].extend(srcs)
        t['proc']['raw_wh'].extend(proc['raw_wh'])
        t['proc']['zone'].extend(proc['zone'])
        t['proc']['fnsku'].extend(proc['fnsku'])
        t['proc']['qty'] += proc['qty']
        if logs: t['logs'].extend([f"[R4采购加工]: {l}" for l in logs])
        
    # --- Round 5: 待下单标记 ---
    for t in tasks:
        if t['filled'] < t['qty']:
            gap = t['qty'] - t['filled']
            t['logs'].append(f"缺口 {to_int(gap)} -> 待下单")
            # if not t['srcs']: t['srcs'].append("待下单")

    # --- 结果整理 ---
    output_rows = []
    
    # 日志
    for t in tasks:
        calc_logs.append({
            "SKU": t['sku'], "FNSKU": t['fnsku'], "需求": to_int(t['qty']),
            "国家": t['country'], "优先级": f"Tier {t['priority']}",
            "执行过程": " || ".join(t['logs']),
            "最终发货": to_int(t['filled'])
        })

    # SKU级缺货Map
    sku_shortage_map = {} 
    for t in tasks:
        gap = t['qty'] - t['filled']
        if gap > 0.001:
            sku_shortage_map[t['sku']] = sku_shortage_map.get(t['sku'], 0) + gap
            
    # 回填
    results_map = {t['row_idx']: t for t in tasks}
    
    for idx, row in df_input.iterrows():
        t = results_map.get(idx)
        out_row = row.to_dict()
        
        if t:
            status_str = "+".join(sorted(set(t['srcs'])))
            if t['filled'] < t['qty']:
                status_str += f"(缺{to_int(t['qty'] - t['filled'])})"
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
                "加工库区": p_wh,
                "加工库区_库位": p_zone,
                "加工FNSKU": p_fn,
                "加工数量": p_qt,
                "剩_深仓": to_int(snap['深仓']),
                "剩_外协": to_int(snap['外协']),
                "剩_云仓": to_int(snap['云仓']),
                "剩_PO": to_int(snap['采购订单'])
            })
        else:
             out_row.update({"库存状态": "-", "最终发货数量": 0, "缺货与否": "-"})
        output_rows.append(out_row)

    df_out = pd.DataFrame(output_rows)
    df_calc_log = pd.DataFrame(calc_logs)
    df_clean_log = pd.DataFrame(inv_mgr.cleaning_logs)
    
    if not df_out.empty and col_sku in df_out.columns:
        df_out.sort_values(by=[col_sku], inplace=True)
        base_cols = list(df_input.columns)
        calc_cols = ["库存状态", "最终发货数量", "缺货与否", 
                     "加工库区", "加工库区_库位", "加工FNSKU", "加工数量", 
                     "剩_深仓", "剩_外协", "剩_云仓", "剩_PO"]
        final_cols = base_cols + [c for c in calc_cols if c not in base_cols]
        df_out = df_out[final_cols]

    return df_out, df_calc_log, df_clean_log

# ==========================================
# 5. UI 渲染
# ==========================================
if 'df_demand' not in st.session_state:
    st.session_state.df_demand = pd.DataFrame(columns=["标签", "国家", "SKU", "FNSKU", "数量", "运营", "店铺", "备注"])

col_main, col_side = st.columns([75, 25])

with col_main:
    st.subheader("1. 需求填报 (V28.0 PO精准版)")
    st.info("💡 请直接粘贴 Excel 数据")
    
    edited_df = st.data_editor(
        st.session_state.df_demand,
        num_rows="dynamic",
        use_container_width=True,
        height=400,
        key="editor"
    )
    
    cols = list(edited_df.columns)
    def get_idx(candidates):
        for i, c in enumerate(cols):
            if c in candidates: return i
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
    f_plan = st.file_uploader("计划表", type=['xlsx', 'xls', 'csv'])
    
    if st.button("🚀 开始计算", type="primary", use_container_width=True):
        if f_inv and f_po and not edited_df.empty:
            with st.spinner("执行四轮全局统筹..."):
                df_inv_raw, err1 = load_and_find_header(f_inv, "库存")
                df_po_raw, err2 = load_and_find_header(f_po, "PO")
                df_plan_raw, _ = load_and_find_header(f_plan, "计划")
                
                if err1: st.error(err1)
                elif err2: st.error(err2)
                else:
                    mgr = InventoryManager(df_inv_raw, df_po_raw)
                    final_df, df_calc, df_clean = run_allocation(edited_df, mgr, df_plan_raw, mapping)
                    
                    st.success("计算完成!")
                    
                    tab1, tab2, tab3 = st.tabs(["📋 分配结果", "🔍 运算日志", "🧹 清洗日志"])
                    
                    with tab1:
                        def highlight(row):
                            if "缺货" in str(row.get('缺货与否', '')): return ['background-color: #ffcdd2'] * len(row)
                            return [''] * len(row)
                        st.dataframe(final_df.style.apply(highlight, axis=1), use_container_width=True)
                        
                    with tab2:
                        st.dataframe(df_calc, use_container_width=True)
                    with tab3:
                        st.dataframe(df_clean, use_container_width=True)
                    
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                        final_df.to_excel(writer, sheet_name='分配结果', index=False)
                        df_calc.to_excel(writer, sheet_name='运算日志', index=False)
                        df_clean.to_excel(writer, sheet_name='清洗日志', index=False)
                        writer.sheets['分配结果'].freeze_panes(1, 0)
                    
                    st.download_button("📥 下载完整结果.xlsx", buf.getvalue(), "V28_Result_Full.xlsx")
        else:
            st.warning("请填写需求数据并上传库存文件")
