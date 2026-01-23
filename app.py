import streamlit as st
import pandas as pd
import io
import copy

# ==========================================
# 1. 基础配置
# ==========================================
st.set_page_config(page_title="智能调拨系统 V9.0 (灵活输入版)", layout="wide", page_icon="🦁")

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
st.title("🦁 智能库存分配 V9.0 (保留任意列+计划选填)")

# ==========================================
# 2. 数据清洗与读取
# ==========================================
def clean_number(x):
    """强制清洗数字"""
    if pd.isna(x): return 0
    s = str(x).strip().replace(',', '').replace(' ', '')
    try: return float(s)
    except: return 0

def load_and_find_header(file, type_tag):
    """自动寻找表头 (鲁棒性读取)"""
    if not file: return None, "未上传"
    try:
        file.seek(0)
        # 预览前15行
        if file.name.endswith('.csv'):
            try: df_preview = pd.read_csv(file, header=None, nrows=15, encoding='utf-8-sig')
            except: 
                file.seek(0)
                df_preview = pd.read_csv(file, header=None, nrows=15, encoding='gbk')
        else:
            df_preview = pd.read_excel(file, header=None, nrows=15)
        
        # 寻找包含 SKU 的行
        header_idx = -1
        for i, row in df_preview.iterrows():
            row_str = " ".join([str(v).upper() for v in row.values])
            if "SKU" in row_str:
                header_idx = i
                break
        
        if header_idx == -1: return None, f"❌ {type_tag}: 未找到包含'SKU'的表头行"
        
        # 重新读取
        file.seek(0)
        if file.name.endswith('.csv'):
            try: df = pd.read_csv(file, header=header_idx, encoding='utf-8-sig')
            except: 
                file.seek(0)
                df = pd.read_csv(file, header=header_idx, encoding='gbk')
        else:
            df = pd.read_excel(file, header=header_idx)
            
        df.columns = [str(c).strip() for c in df.columns]
        df.dropna(how='all', inplace=True)
        return df, None
    except Exception as e:
        return None, f"❌ {type_tag} 读取出错: {str(e)}"

def smart_col(df, candidates):
    cols = list(df.columns)
    for c in cols:
        if c in candidates: return c
    for cand in candidates:
        for c in cols:
            if cand in c: return c
    return None

# ==========================================
# 3. 核心：库存管理器
# ==========================================
class InventoryManager:
    def __init__(self, df_inv, df_po):
        # 动态库存 (随分配减少)
        self.stock = {} 
        self.po = {}
        
        # 原始库存 (只读备份，用于验证展示)
        self.orig_stock = {}
        self.orig_po = {}
        
        self.stats = {'inv_rows': 0, 'po_rows': 0, 'total_stock': 0, 'total_po': 0}
        
        self._init_inventory(df_inv)
        self._init_po(df_po)
        
        # 创建原始数据备份
        self.orig_stock = copy.deepcopy(self.stock)
        self.orig_po = copy.deepcopy(self.po)

    def _get_wh_type(self, wh_name):
        n = str(wh_name).strip()
        if "亚马逊深圳仓" in n or "深仓" in n: return "深仓"
        if "亚马逊外协" in n or "外协" in n: return "外协"
        if "云仓" in n or "天源" in n: return "云仓"
        return "其他"

    def _init_inventory(self, df):
        self.stats['inv_rows'] = len(df)
        for _, row in df.iterrows():
            s = str(row.get('SKU', '')).strip()
            f_raw = row.get('FNSKU', '')
            f = str(f_raw).strip() if pd.notna(f_raw) else ""
            w_name = str(row.get('仓库名称', ''))
            q = clean_number(row.get('可用库存', 0))
            
            if q <= 0 or not s: continue
            
            w_type = self._get_wh_type(w_name)
            self.stats['total_stock'] += q
            
            if s not in self.stock: self.stock[s] = {}
            if f not in self.stock[s]: self.stock[s][f] = {'深仓':0, '外协':0, '云仓':0, '其他':0}
            self.stock[s][f][w_type] = self.stock[s][f].get(w_type, 0) + q

    def _init_po(self, df):
        self.stats['po_rows'] = len(df)
        for _, row in df.iterrows():
            s = str(row.get('SKU', '')).strip()
            q = clean_number(row.get('未入库量', 0))
            if q > 0 and s:
                self.po[s] = self.po.get(s, 0) + q
                self.stats['total_po'] += q

    # --- 快照功能 ---
    def get_sku_snapshot(self, sku, use_original=False):
        """获取某SKU各维度库存"""
        res = {'外协': 0, '云仓': 0, '深仓': 0, 'PO': 0}
        target_stock = self.orig_stock if use_original else self.stock
        target_po = self.orig_po if use_original else self.po
        
        if sku in target_stock:
            for f_key in target_stock[sku]:
                for w_type in ['外协', '云仓', '深仓']:
                    res[w_type] += target_stock[sku][f_key].get(w_type, 0)
        res['PO'] = target_po.get(sku, 0)
        return res

    # --- 巡检 ---
    def check_max_availability(self, sku, target_fnsku, src_type, src_name):
        total_avail = 0
        if src_type == 'stock':
            if sku in self.stock:
                if target_fnsku in self.stock[sku]:
                    total_avail += self.stock[sku][target_fnsku].get(src_name, 0)
                for other_f in self.stock[sku]:
                    if other_f == target_fnsku: continue
                    total_avail += self.stock[sku][other_f].get(src_name, 0)
        elif src_type == 'po':
            if sku in self.po:
                total_avail += self.po[sku]
        return total_avail

    # --- 执行 ---
    def execute_deduction(self, sku, target_fnsku, qty_needed, strategy_chain):
        qty_remain = qty_needed
        breakdown_notes = []
        used_sources = []
        
        for src_type, src_name in strategy_chain:
            if qty_remain <= 0: break
            step_taken = 0
            
            if src_type == 'stock':
                if sku in self.stock and target_fnsku in self.stock[sku]:
                    avail = self.stock[sku][target_fnsku].get(src_name, 0)
                    take = min(avail, qty_remain)
                    if take > 0:
                        self.stock[sku][target_fnsku][src_name] -= take
                        qty_remain -= take
                        step_taken += take
                        
                if qty_remain > 0 and sku in self.stock:
                    for other_f in self.stock[sku]:
                        if other_f == target_fnsku: continue
                        if qty_remain <= 0: break
                        avail = self.stock[sku][other_f].get(src_name, 0)
                        take = min(avail, qty_remain)
                        if take > 0:
                            self.stock[sku][other_f][src_name] -= take
                            qty_remain -= take
                            step_taken += take
                            breakdown_notes.append(f"{src_name}加工(用{other_f}补{take})")
            
            elif src_type == 'po':
                if sku in self.po:
                    avail = self.po[sku]
                    take = min(avail, qty_remain)
                    if take > 0:
                        self.po[sku] -= take
                        qty_remain -= take
                        step_taken += take
            
            if step_taken > 0:
                if src_name not in used_sources:
                    used_sources.append(src_name)
        
        filled_qty = qty_needed - qty_remain
        return filled_qty, breakdown_notes, used_sources

# ==========================================
# 4. 逻辑控制
# ==========================================
def get_strategy_priority(country_str):
    c = str(country_str).upper().strip()
    is_us = 'US' in c or '美国' in c
    if is_us:
        return [('stock', '外协'), ('stock', '云仓'), ('stock', '深仓'), ('po', '采购订单')], True
    else:
        return [('stock', '深仓'), ('stock', '外协'), ('stock', '云仓'), ('po', '采购订单')], False

def smart_allocate(mgr, sku, fnsku, qty, country):
    base_priority, is_us = get_strategy_priority(country)
    final_strategy = []
    
    if is_us:
        atomic_source_found = None
        for src_type, src_name in base_priority:
            max_avail = mgr.check_max_availability(sku, fnsku, src_type, src_name)
            if max_avail >= qty - 0.001:
                atomic_source_found = (src_type, src_name)
                break
        
        if atomic_source_found:
            final_strategy = [atomic_source_found]
        else:
            final_strategy = base_priority
    else:
        final_strategy = base_priority

    return mgr.execute_deduction(sku, fnsku, qty, final_strategy)

def run_full_process(df_demand, inv_mgr, df_plan):
    # 0. 预计算计划表汇总
    plan_summary_dict = {} 
    
    # 1. 计划表预扣减 (如果没上传 df_plan，这一步自动跳过)
    if df_plan is not None and not df_plan.empty:
        p_sku = smart_col(df_plan, ['SKU', 'sku'])
        p_fnsku = smart_col(df_plan, ['FNSKU', 'FnSKU'])
        p_qty = smart_col(df_plan, ['需求', '计划', '数量'])
        p_country = smart_col(df_plan, ['国家', 'Country']) 
        
        if p_sku and p_qty:
            for _, row in df_plan.iterrows():
                sku = str(row[p_sku]).strip()
                f_raw = row[p_fnsku] if p_fnsku else ""
                fnsku = str(f_raw).strip() if pd.notna(f_raw) else ""
                qty = clean_number(row[p_qty])
                
                if qty <= 0: continue
                
                plan_summary_dict[sku] = plan_summary_dict.get(sku, 0) + qty
                
                cty = str(row[p_country]) if p_country else "Non-US"
                smart_allocate(inv_mgr, sku, fnsku, qty, cty)

    # 2. 需求分配 (处理输入数据)
    df = df_demand.copy()
    
    # 识别核心列
    col_sku = smart_col(df, ['SKU', 'sku'])
    col_qty = smart_col(df, ['需求数量', '数量', 'Qty', '需求'])
    col_tag = smart_col(df, ['标签列', '标签', 'Tag'])
    col_country = smart_col(df, ['国家', 'Country'])
    col_fnsku = smart_col(df, ['FNSKU', 'FnSKU', 'fnsku'])
    
    # 如果找不到列，返回错误
    if not (col_sku and col_qty and col_tag and col_country):
        st.error(f"❌ 需求表缺少关键列，请检查表头。需包含: SKU, 数量, 标签, 国家")
        return pd.DataFrame()

    df['calc_qty'] = df[col_qty].apply(clean_number)
    df = df[df['calc_qty'] > 0]
    
    def get_sort_key(row):
        tag = str(row.get(col_tag, '')).strip()
        cty = str(row.get(col_country, '')).strip().upper()
        base_score = 10 if '新增' in tag else 30
        country_offset = 1 if ('US' in cty or '美国' in cty) else 0
        return base_score + country_offset

    df['sort_key'] = df.apply(get_sort_key, axis=1)
    # 按 SKU 聚类，再按优先级
    df_sorted = df.sort_values(by=[col_sku, 'sort_key', col_country])
    
    results = []
    for idx, row in df_sorted.iterrows():
        # 提取数据
        sku = str(row[col_sku]).strip()
        f_raw = row.get(col_fnsku, '')
        fnsku = str(f_raw).strip() if pd.notna(f_raw) else ""
        country = str(row[col_country]).strip()
        qty_needed = row['calc_qty']
        
        # 执行分配
        filled, notes, sources = smart_allocate(inv_mgr, sku, fnsku, qty_needed, country)
        
        status = ""
        wait_qty = qty_needed - filled
        if wait_qty < 0.001:
            status = "+".join(sources) if sources else "库存异常"
        elif filled > 0:
            status = f"部分缺货(缺{wait_qty:g}):{'+'.join(sources)}"
        else:
            status = f"待下单(需{qty_needed:g})"
            
        orig = inv_mgr.get_sku_snapshot(sku, use_original=True)
        plan_total = plan_summary_dict.get(sku, 0)
        
        # === 关键修改：保留用户的所有原始列，并追加新列 ===
        res_row = row.to_dict()
        
        # 移除临时计算列
        if 'sort_key' in res_row: del res_row['sort_key']
        if 'calc_qty' in res_row: del res_row['calc_qty']
        
        # 追加计算结果
        res_row.update({
            "SKU": sku, # 确保核心列在
            "FNSKU": fnsku,
            "最终发货数量": filled,
            "订单状态": status, 
            "备注": "; ".join(notes),
            "原始外协": orig['外协'],
            "原始云仓": orig['云仓'],
            "原始深仓": orig['深仓'],
            "原始PO": orig['PO'],
            "提货计划汇总": plan_total
        })
        results.append(res_row)
        
    return pd.DataFrame(results)

# ==========================================
# 5. UI 界面
# ==========================================
col_left, col_right = st.columns([35, 65])

with col_left:
    st.subheader("1. 需求输入")
    
    # === 新增功能：输入方式选择 ===
    input_method = st.radio("选择输入方式:", ["手动录入 (基础列)", "文件上传 (保留任意列)"], horizontal=True)
    
    df_input = None
    
    if input_method == "手动录入 (基础列)":
        col_cfg = {
            "标签列": st.column_config.SelectboxColumn("标签列", options=["新增需求", "当周需求"], required=True),
            "需求数量": st.column_config.NumberColumn("需求数量", required=True, min_value=0),
        }
        sample = pd.DataFrame([{"标签列": "新增需求", "国家": "DE", "SKU": "A001", "FNSKU": "X1", "需求数量": 80}])
        df_input = st.data_editor(sample, column_config=col_cfg, num_rows="dynamic", height=450, use_container_width=True)
    else:
        # 文件上传模式
        up_file = st.file_uploader("📤 上传需求表格 (支持自定义列)", type=['xlsx', 'xls', 'csv'])
        if up_file:
            df_input, _ = load_and_find_header(up_file, "需求表")
            if df_input is not None:
                st.success(f"已加载 {len(df_input)} 行数据，包含列: {list(df_input.columns)}")
                st.dataframe(df_input.head(3), use_container_width=True, height=150)

with col_right:
    st.subheader("2. 库存文件上传")
    st.info("💡 提货计划为【选填】，如有上传则优先扣减。")
    
    f_inv = st.file_uploader("📂 A. 在库库存表 (必填)", type=['xlsx', 'xls', 'csv'])
    f_po = st.file_uploader("📂 B. 采购订单追踪表 (必填)", type=['xlsx', 'xls', 'csv'])
    # === 修改说明：明确标记为选填 ===
    f_plan = st.file_uploader("📂 C. 提货需求表 (选填 - 用于预扣减)", type=['xlsx', 'xls', 'csv'])
    
    st.divider()
    
    if st.button("🚀 开始运算", type="primary", use_container_width=True):
        # === 修改说明：f_plan 不再强制 ===
        if not (f_inv and f_po):
            st.error("❌ 请至少上传【库存表】和【采购表】！")
        elif df_input is None or df_input.empty:
            st.error("❌ 请输入或上传需求数据！")
        else:
            with st.spinner("正在读取并进行全链路计算..."):
                try:
                    df_inv_raw, err1 = load_and_find_header(f_inv, "库存表")
                    df_po_raw, err2 = load_and_find_header(f_po, "采购表")
                    
                    # 提货计划是选填的
                    df_plan_raw = None
                    if f_plan:
                        df_plan_raw, err3 = load_and_find_header(f_plan, "计划表")
                    
                    if err1 or err2:
                        st.error(f"{err1 or ''} \n {err2 or ''}")
                    else:
                        inv_map = {
                            smart_col(df_inv_raw, ['SKU', 'sku']): 'SKU',
                            smart_col(df_inv_raw, ['FNSKU', 'FnSKU']): 'FNSKU',
                            smart_col(df_inv_raw, ['仓库', '仓库名称']): '仓库名称',
                            smart_col(df_inv_raw, ['可用', '可用库存']): '可用库存'
                        }
                        po_map = {
                            smart_col(df_po_raw, ['SKU', 'sku']): 'SKU',
                            smart_col(df_po_raw, ['未入库', '未入库量']): '未入库量'
                        }
                        
                        df_inv_clean = df_inv_raw.rename(columns=inv_map)
                        df_po_clean = df_po_raw.rename(columns=po_map)
                        
                        mgr = InventoryManager(df_inv_clean, df_po_clean)
                        
                        # 数据自检
                        st.success(f"📊 数据自检: 识别库存 {mgr.stats['total_stock']:,.0f} | PO {mgr.stats['total_po']:,.0f}")
                        
                        if mgr.stats['total_stock'] == 0:
                            st.warning("⚠️ 警告：未识别到库存，请检查Excel文件表头。")
                        
                        # 运行计算
                        final_df = run_full_process(df_input, mgr, df_plan_raw)
                        
                        if final_df.empty:
                            st.warning("无有效结果 (可能是SKU未匹配或数量为0)")
                        else:
                            st.dataframe(final_df, use_container_width=True)
                            buf = io.BytesIO()
                            with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                                final_df.to_excel(writer, index=False)
                            st.download_button("📥 下载 V9.0 结果.xlsx", buf.getvalue(), "V9_Allocation.xlsx", "application/vnd.ms-excel")

                except Exception as e:
                    st.error(f"运行错误: {str(e)}")
