import streamlit as st
import pandas as pd
import io
import copy

# ==========================================
# 1. 基础配置
# ==========================================
st.set_page_config(page_title="智能调拨系统 V21.0 (过程透明版)", layout="wide", page_icon="🦁")

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
st.title("🦁 智能库存分配 V21.0 (深度清洗 + 计算过程表)")

# ==========================================
# 2. 数据清洗与辅助函数
# ==========================================
def clean_number(x):
    """强制清洗为数字"""
    if pd.isna(x): return 0
    # 替换掉逗号，去除空格
    s = str(x).replace(',', '').strip()
    try: return float(s)
    except: return 0

def to_int(x):
    """四舍五入转整数"""
    try: return int(round(float(x)))
    except: return 0

def normalize_str(s):
    """标准化字符串：去空、转大写"""
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
            
        # 寻找表头
        header_idx = -1
        for i, row in df.head(30).iterrows(): # 增加搜索范围到30行
            row_str = " ".join([str(v).upper() for v in row.values])
            if "SKU" in row_str:
                header_idx = i
                break
        
        if header_idx != -1:
            df.columns = df.iloc[header_idx]
            df = df.iloc[header_idx+1:]
        
        # 重置索引
        df.reset_index(drop=True, inplace=True)
        # 去除列名的空格
        df.columns = [str(c).strip() for c in df.columns]
        # 去除全空行
        df.dropna(how='all', inplace=True)
        return df, None
    except Exception as e:
        return None, f"读取错误: {str(e)}"

# ==========================================
# 3. 核心：库存管理器
# ==========================================
class InventoryManager:
    def __init__(self, df_inv, df_po):
        self.stock = {} 
        self.po = {}
        # 统计数据清洗情况
        self.stats = {
            'inv_total_rows': 0, 'inv_filtered': 0, 'inv_valid': 0,
            'po_total_rows': 0, 'po_filtered': 0, 'po_valid': 0
        }
        
        self._init_inventory(df_inv)
        self._init_po(df_po)
        # 原始快照用于计算剩余
        self.orig_stock = copy.deepcopy(self.stock)
        self.orig_po = copy.deepcopy(self.po)

    def _init_inventory(self, df):
        if df is None or df.empty: return
        self.stats['inv_total_rows'] = len(df)
        
        # 模糊匹配列名
        c_sku = next((c for c in df.columns if 'SKU' in c.upper()), None)
        c_fnsku = next((c for c in df.columns if 'FNSKU' in c.upper()), None)
        c_wh = next((c for c in df.columns if '仓库' in c), None)
        c_qty = next((c for c in df.columns if '数量' in c or '库存' in c), None)

        if not (c_sku and c_wh and c_qty): return

        for _, row in df.iterrows():
            w_name_raw = str(row.get(c_wh, ''))
            w_name_norm = normalize_str(w_name_raw)
            
            # === 黑名单过滤 (严格) ===
            # 只要包含 WALMART, 沃尔玛, TEMU 就过滤
            blacklist_keywords = ["沃尔玛", "WALMART", "TEMU"]
            if any(k in w_name_norm for k in blacklist_keywords):
                self.stats['inv_filtered'] += 1
                continue
            
            sku = str(row.get(c_sku, '')).strip()
            if not sku: 
                self.stats['inv_filtered'] += 1
                continue
            
            f_raw = row.get(c_fnsku, '')
            fnsku = str(f_raw).strip() if pd.notna(f_raw) else ""
            qty = clean_number(row.get(c_qty, 0))
            
            if qty <= 0: 
                self.stats['inv_filtered'] += 1
                continue
            
            # 标准化仓库类型
            w_type = normalize_wh_name(w_name_raw)
            
            self.stats['inv_valid'] += 1
            
            if sku not in self.stock: self.stock[sku] = {}
            if fnsku not in self.stock[sku]: self.stock[sku][fnsku] = {'深仓':0, '外协':0, '云仓':0, '采购订单':0, '其他':0}
            self.stock[sku][fnsku][w_type] = self.stock[sku][fnsku].get(w_type, 0) + qty

    def _init_po(self, df):
        if df is None or df.empty: return
        self.stats['po_total_rows'] = len(df)
        
        c_sku = next((c for c in df.columns if 'SKU' in c.upper()), None)
        c_qty = next((c for c in df.columns if '未入库' in c or '数量' in c), None)
        c_req = next((c for c in df.columns if '人' in c or '员' in c), None)
        
        # === 人员黑名单 ===
        block_list = ["陈丹丹", "张萍", "杨上儒", "陈炜填", "贝少婷", "詹翠萍"]
        
        for _, row in df.iterrows():
            if c_req:
                req_raw = str(row.get(c_req, ''))
                # 只要包含黑名单名字就过滤 (模糊匹配)
                if any(b in req_raw for b in block_list): 
                    self.stats['po_filtered'] += 1
                    continue
                
            sku = str(row.get(c_sku, '')).strip()
            qty = clean_number(row.get(c_qty, 0))
            
            if sku and qty > 0:
                self.po[sku] = self.po.get(sku, 0) + qty
                self.stats['po_valid'] += 1
            else:
                self.stats['po_filtered'] += 1

    def get_snapshot(self, sku):
        """获取当前库存快照 (用于日志和最终剩余)"""
        res = {'深仓':0, '外协':0, '云仓':0, '采购订单': self.po.get(sku, 0)}
        if sku in self.stock:
            for f in self.stock[sku]:
                for w in res.keys():
                    res[w] += self.stock[sku][f].get(w, 0)
        return res

    def find_whole_match(self, sku, target_fnsku, qty, candidates):
        """寻找整单满足的仓库"""
        for src_type, src_name in candidates:
            total_avail = 0
            if src_type == 'stock':
                if sku in self.stock:
                    for f in self.stock[sku]:
                        total_avail += self.stock[sku][f].get(src_name, 0)
            elif src_type == 'po':
                total_avail = self.po.get(sku, 0)
            
            if total_avail >= qty:
                return [(src_type, src_name)] 
        return None

    def execute_deduction(self, sku, target_fnsku, qty_needed, strategy_chain):
        """执行扣减"""
        qty_remain = qty_needed
        breakdown_notes = []
        used_sources = []
        process_details = {'wh': [], 'fnsku': [], 'qty': 0}
        deduction_log = [] # 记录具体的扣减动作供日志使用
        
        for src_type, src_name in strategy_chain:
            if qty_remain <= 0: break
            
            take_total = 0
            
            if src_type == 'stock':
                if sku in self.stock:
                    # A. 同 FNSKU
                    if target_fnsku in self.stock[sku]:
                        avail = self.stock[sku][target_fnsku].get(src_name, 0)
                        take = min(avail, qty_remain)
                        if take > 0:
                            self.stock[sku][target_fnsku][src_name] -= take
                            qty_remain -= take
                            take_total += take
                            deduction_log.append(f"{src_name}(-{to_int(take)})")
                    
                    # B. 加工
                    if qty_remain > 0:
                        for other_f in self.stock[sku]:
                            if other_f == target_fnsku: continue
                            if qty_remain <= 0: break
                            
                            avail = self.stock[sku][other_f].get(src_name, 0)
                            take = min(avail, qty_remain)
                            if take > 0:
                                self.stock[sku][other_f][src_name] -= take
                                qty_remain -= take
                                take_total += take
                                # 记录加工
                                breakdown_notes.append(f"{src_name}(加工)")
                                process_details['wh'].append(src_name)
                                process_details['fnsku'].append(other_f)
                                process_details['qty'] += take
                                deduction_log.append(f"{src_name}加工(-{to_int(take)})")
                                
            elif src_type == 'po':
                avail = self.po.get(sku, 0)
                take = min(avail, qty_remain)
                if take > 0:
                    self.po[sku] -= take
                    qty_remain -= take
                    take_total += take
                    deduction_log.append(f"PO(-{to_int(take)})")
            
            if take_total > 0:
                used_sources.append(src_name)

        return qty_needed - qty_remain, breakdown_notes, used_sources, process_details, deduction_log

# ==========================================
# 4. 主逻辑流程 (含过程记录)
# ==========================================
def run_allocation(df_input, inv_mgr, df_plan, mapping):
    tasks = []
    # 过程日志表数据
    process_logs = [] 
    
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
                    # 记录快照
                    snap_before = inv_mgr.get_snapshot(sku)
                    strat = [('stock', '深仓'), ('stock', '外协'), ('stock', '云仓'), ('po', '采购订单')]
                    filled, _, _, _, logs = inv_mgr.execute_deduction(sku, fnsku, qty, strat)
                    
                    process_logs.append({
                        "SKU": sku, "类型": "提货计划", "国家": cty, "需求": to_int(qty),
                        "策略": "Non-US瀑布流",
                        "前_深仓": to_int(snap_before['深仓']),
                        "前_外协": to_int(snap_before['外协']),
                        "前_云仓": to_int(snap_before['云仓']),
                        "前_PO": to_int(snap_before['采购订单']),
                        "执行过程": "; ".join(logs),
                        "剩余需求": to_int(qty - filled)
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
        if is_new:
            priority = 2 if is_us else 1
        else:
            priority = 4 if is_us else 3
            
        tasks.append({
            'row_idx': idx, 'priority': priority,
            'sku': sku, 'fnsku': fnsku, 'qty': qty, 'country': country,
            'is_us': is_us, 'tag': tag
        })

    # --- 3. 执行分配 ---
    tasks.sort(key=lambda x: x['priority'])
    
    results = {} 
    
    for t in tasks:
        rid = t['row_idx']
        sku = t['sku']
        fnsku = t['fnsku']
        qty = t['qty']
        is_us = t['is_us']
        
        # 记录快照
        snap_before = inv_mgr.get_snapshot(sku)
        strat_name = ""
        deduction_logs = []
        
        if not is_us:
            # Non-US
            strat_name = "瀑布流(深>外>云>PO)"
            strat = [('stock', '深仓'), ('stock', '外协'), ('stock', '云仓'), ('po', '采购订单')]
            filled, notes, srcs, proc, d_logs = inv_mgr.execute_deduction(sku, fnsku, qty, strat)
            deduction_logs = d_logs
        else:
            # US
            strat_name = "US整单优先"
            candidates = [('stock', '外协'), ('stock', '云仓'), ('po', '采购订单'), ('stock', '深仓')]
            whole_match_strat = inv_mgr.find_whole_match(sku, fnsku, qty, candidates)
            
            if whole_match_strat:
                filled, notes, srcs, proc, d_logs = inv_mgr.execute_deduction(sku, fnsku, qty, whole_match_strat)
                strat_name += "(命中整单)"
                deduction_logs = d_logs
            else:
                filled = 0
                notes = ["无整单满足，待下单"]
                srcs = []
                proc = {'wh': [], 'fnsku': [], 'qty': 0}
                strat_name += "(未命中->待下单)"
                deduction_logs = ["库存不足以整单满足"]

        # 记录过程表
        process_logs.append({
            "SKU": sku, "类型": t['tag'], "国家": t['country'], "需求": to_int(qty),
            "策略": strat_name,
            "前_深仓": to_int(snap_before['深仓']),
            "前_外协": to_int(snap_before['外协']),
            "前_云仓": to_int(snap_before['云仓']),
            "前_PO": to_int(snap_before['采购订单']),
            "执行过程": "; ".join(deduction_logs),
            "剩余需求": to_int(qty - filled)
        })

        results[rid] = {
            'filled': filled, 'notes': notes, 'srcs': srcs, 'proc': proc
        }

    # --- 4. 构建主输出 ---
    output_rows = []
    
    # SKU级缺货
    sku_shortage_map = {} 
    for idx, row in df_input.iterrows():
        qty = clean_number(row.get(col_qty, 0))
        if idx in results:
            short = qty - results[idx]['filled']
            if short > 0.001:
                sku = str(row.get(col_sku, '')).strip()
                sku_shortage_map[sku] = sku_shortage_map.get(sku, 0) + short
    
    for idx, row in df_input.iterrows():
        res = results.get(idx)
        out_row = row.to_dict()
        
        if res:
            filled = res['filled']
            status_str = "+".join(sorted(set(res['srcs']))) if res['srcs'] else "待下单"
            p_wh = ";".join(set(res['proc']['wh']))
            p_fn = ";".join(res['proc']['fnsku'])
            p_qt = to_int(res['proc']['qty']) if res['proc']['qty'] > 0 else ""
            
            sku = str(row.get(col_sku, '')).strip()
            snap = inv_mgr.get_snapshot(sku)
            
            total_sku_short = sku_shortage_map.get(sku, 0)
            short_status = f"❌ 缺货 (该SKU总缺 {to_int(total_sku_short)})" if total_sku_short > 0 else "✅ 全满足"
            
            out_row.update({
                "库存状态": status_str,
                "最终发货数量": to_int(filled),
                "缺货与否": short_status,
                "加工库区": p_wh,
                "加工FNSKU": p_fn,
                "加工数量": p_qt,
                "剩_深仓": to_int(snap['深仓']),
                "剩_外协": to_int(snap['外协']),
                "剩_云仓": to_int(snap['云仓']),
                "剩_PO": to_int(snap['采购订单'])
            })
        else:
            out_row.update({
                "库存状态": "-", "最终发货数量": 0, "缺货与否": "-",
                "加工库区": "", "加工FNSKU": "", "加工数量": "",
                "剩_深仓":0, "剩_外协":0, "剩_云仓":0, "剩_PO":0
            })
        output_rows.append(out_row)

    df_out = pd.DataFrame(output_rows)
    df_process = pd.DataFrame(process_logs)
    
    if not df_out.empty and col_sku in df_out.columns:
        df_out.sort_values(by=[col_sku], inplace=True)
        base_cols = list(df_input.columns)
        calc_cols = [
            "库存状态", "最终发货数量", "缺货与否", 
            "加工库区", "加工FNSKU", "加工数量", 
            "剩_深仓", "剩_外协", "剩_云仓", "剩_PO"
        ]
        final_cols = base_cols + [c for c in calc_cols if c not in base_cols]
        df_out = df_out[final_cols]

    return df_out, df_process

# ==========================================
# 5. UI 渲染
# ==========================================
if 'df_demand' not in st.session_state:
    st.session_state.df_demand = pd.DataFrame(columns=["标签", "国家", "SKU", "FNSKU", "数量", "运营", "店铺", "备注"])

col_main, col_side = st.columns([75, 25])

with col_main:
    st.subheader("1. 需求填报")
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
            for cand in candidates:
                if cand in c: return i
        return 0

    st.write("🔧 **列映射配置**")
    c1, c2, c3, c4, c5 = st.columns(5)
    map_tag = c1.selectbox("标签列", cols, index=get_idx(['标签', 'Tag']))
    map_country = c2.selectbox("国家列", cols, index=get_idx(['国家', 'Country']))
    map_sku = c3.selectbox("SKU列", cols, index=get_idx(['SKU', 'sku']))
    map_fnsku = c4.selectbox("FNSKU列", cols, index=get_idx(['FNSKU', 'FnSKU']))
    map_qty = c5.selectbox("数量列", cols, index=get_idx(['数量', 'Qty']))
    
    mapping = {'标签': map_tag, '国家': map_country, 'SKU': map_sku, 'FNSKU': map_fnsku, '数量': map_qty}

with col_side:
    st.subheader("2. 库存文件")
    f_inv = st.file_uploader("库存表", type=['xlsx', 'xls', 'csv'])
    f_po = st.file_uploader("PO表", type=['xlsx', 'xls', 'csv'])
    f_plan = st.file_uploader("计划表 (选填)", type=['xlsx', 'xls', 'csv'])
    
    st.divider()
    
    if st.button("🚀 开始计算", type="primary", use_container_width=True):
        if f_inv and f_po and not edited_df.empty:
            with st.spinner("计算中..."):
                df_inv_raw, err1 = load_and_find_header(f_inv, "库存")
                df_po_raw, err2 = load_and_find_header(f_po, "PO")
                df_plan_raw, _ = load_and_find_header(f_plan, "计划")
                
                if err1: st.error(err1)
                elif err2: st.error(err2)
                else:
                    mgr = InventoryManager(df_inv_raw, df_po_raw)
                    
                    # 显示数据清洗统计
                    st.info(f"""
                    📊 **数据读取报告**:
                    - **库存表**: 原始 {mgr.stats['inv_total_rows']} 行, 有效 {mgr.stats['inv_valid']} 行 (过滤 {mgr.stats['inv_filtered']} 行: 黑名单/空数据)
                    - **PO 表**: 原始 {mgr.stats['po_total_rows']} 行, 有效 {mgr.stats['po_valid']} 行 (过滤 {mgr.stats['po_filtered']} 行: 黑名单/空数据)
                    """)
                    
                    final_df, process_df = run_allocation(edited_df, mgr, df_plan_raw, mapping)
                    
                    st.success("计算完成!")
                    
                    tab_res, tab_proc = st.tabs(["📋 分配结果", "🔍 计算过程 (Verfication)"])
                    
                    with tab_res:
                        def highlight(row):
                            if "缺货" in str(row.get('缺货与否', '')):
                                return ['background-color: #ffcdd2'] * len(row)
                            return [''] * len(row)
                        st.dataframe(final_df.style.apply(highlight, axis=1), use_container_width=True)
                    
                    with tab_proc:
                        st.dataframe(process_df, use_container_width=True)
                    
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                        final_df.to_excel(writer, sheet_name='分配结果', index=False)
                        process_df.to_excel(writer, sheet_name='计算过程', index=False)
                        writer.sheets['分配结果'].freeze_panes(1, 0)
                    
                    st.download_button("📥 下载结果 (含过程表).xlsx", buf.getvalue(), "V21_Result.xlsx")
        else:
            st.warning("请填写需求数据并上传库存文件")
