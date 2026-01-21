import streamlit as st
import pandas as pd
import io
import re

# ==========================================
# 1. 基础配置
# ==========================================
st.set_page_config(page_title="智能调拨系统 V7.0 (数据修正版)", layout="wide", page_icon="🦁")

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
st.title("🦁 智能库存分配 V7.0 (含数据自检)")

# ==========================================
# 2. 增强型文件读取 (关键修复)
# ==========================================
def clean_number(x):
    """强制清洗数字，处理逗号、空格、非数字字符"""
    if pd.isna(x): return 0
    s = str(x).strip().replace(',', '').replace(' ', '')
    # 提取数字
    try:
        return float(s)
    except:
        return 0

def load_and_find_header(file, type_tag):
    """
    自动扫描前10行，寻找包含'SKU'的行作为真正的表头
    解决Excel有标题行导致读取失败的问题
    """
    if not file: return None, "未上传"
    
    try:
        file.seek(0)
        # 先按不含表头读取前15行
        if file.name.endswith('.csv'):
            try:
                df_preview = pd.read_csv(file, header=None, nrows=15, encoding='utf-8-sig')
            except:
                file.seek(0)
                df_preview = pd.read_csv(file, header=None, nrows=15, encoding='gbk')
        else:
            df_preview = pd.read_excel(file, header=None, nrows=15)
        
        # 扫描寻找表头
        header_idx = -1
        for i, row in df_preview.iterrows():
            row_str = " ".join([str(v).upper() for v in row.values])
            if "SKU" in row_str:
                header_idx = i
                break
        
        if header_idx == -1:
            return None, f"❌ {type_tag}: 前15行未找到包含'SKU'的列，请检查文件格式。"
        
        # 重新读取，指定header行
        file.seek(0)
        if file.name.endswith('.csv'):
            try:
                df = pd.read_csv(file, header=header_idx, encoding='utf-8-sig')
            except:
                file.seek(0)
                df = pd.read_csv(file, header=header_idx, encoding='gbk')
        else:
            df = pd.read_excel(file, header=header_idx)
            
        # 标准化列名（去除前后空格）
        df.columns = [str(c).strip() for c in df.columns]
        
        # 移除全空行
        df.dropna(how='all', inplace=True)
        
        return df, None
        
    except Exception as e:
        return None, f"❌ {type_tag} 读取出错: {str(e)}"

def smart_col(df, candidates):
    """智能模糊匹配列名"""
    cols = list(df.columns)
    # 1. 优先完全匹配
    for c in cols:
        if c in candidates: return c
    # 2. 模糊匹配
    for cand in candidates:
        for c in cols:
            if cand in c: return c
    return None

# ==========================================
# 3. 核心：库存管理器
# ==========================================
class InventoryManager:
    def __init__(self, df_inv, df_po):
        self.stock = {} # stock[sku][fnsku][wh_type] = qty
        self.po = {}    # po[sku] = qty
        
        # 统计数据（用于自检）
        self.stats = {'inv_rows': 0, 'po_rows': 0, 'total_stock': 0, 'total_po': 0}
        
        self._init_inventory(df_inv)
        self._init_po(df_po)

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
            # 兼容空FNSKU的情况
            f_raw = row.get('FNSKU', '')
            f = str(f_raw).strip() if pd.notna(f_raw) else ""
            
            w_name = str(row.get('仓库名称', ''))
            
            # 关键：强力清洗数字
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

    def get_sku_snapshot(self, sku):
        """快照"""
        res = {'外协': 0, '云仓': 0, '深仓': 0, 'PO': 0}
        if sku in self.stock:
            for f_key in self.stock[sku]:
                for w_type in ['外协', '云仓', '深仓']:
                    res[w_type] += self.stock[sku][f_key].get(w_type, 0)
        res['PO'] = self.po.get(sku, 0)
        return res

    # --- 巡检 (Check) ---
    def check_max_availability(self, sku, target_fnsku, src_type, src_name):
        total_avail = 0
        if src_type == 'stock':
            if sku in self.stock:
                # 精确
                if target_fnsku in self.stock[sku]:
                    total_avail += self.stock[sku][target_fnsku].get(src_name, 0)
                # 加工
                for other_f in self.stock[sku]:
                    if other_f == target_fnsku: continue
                    total_avail += self.stock[sku][other_f].get(src_name, 0)
        elif src_type == 'po':
            if sku in self.po:
                total_avail += self.po[sku]
        return total_avail

    # --- 执行 (Deduct) ---
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
        # US 整单优先模式
        atomic_source_found = None
        for src_type, src_name in base_priority:
            max_avail = mgr.check_max_availability(sku, fnsku, src_type, src_name)
            # 浮点数比较，防止精度问题
            if max_avail >= qty - 0.001:
                atomic_source_found = (src_type, src_name)
                break
        
        if atomic_source_found:
            final_strategy = [atomic_source_found]
        else:
            final_strategy = base_priority
    else:
        # Non-US 混合补足模式
        final_strategy = base_priority

    return mgr.execute_deduction(sku, fnsku, qty, final_strategy)

def run_full_process(df_demand, inv_mgr, df_plan):
    # 1. 计划表预扣减
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
                cty = str(row[p_country]) if p_country else "Non-US"
                smart_allocate(inv_mgr, sku, fnsku, qty, cty)

    # 2. 需求分配
    df = df_demand.copy()
    df['需求数量'] = df['需求数量'].apply(clean_number)
    df = df[df['需求数量'] > 0]
    
    def get_sort_key(row):
        tag = str(row.get('标签列', '')).strip()
        cty = str(row.get('国家', '')).strip().upper()
        base_score = 10 if '新增' in tag else 30
        country_offset = 1 if ('US' in cty or '美国' in cty) else 0
        return base_score + country_offset

    df['sort_key'] = df.apply(get_sort_key, axis=1)
    df_sorted = df.sort_values(by=['SKU', 'sort_key', '国家'])
    
    results = []
    for idx, row in df_sorted.iterrows():
        sku = str(row['SKU']).strip()
        f_raw = row['FNSKU']
        fnsku = str(f_raw).strip() if pd.notna(f_raw) else ""
        country = str(row['国家']).strip()
        qty_needed = row['需求数量']
        tag = row['标签列']
        
        filled, notes, sources = smart_allocate(inv_mgr, sku, fnsku, qty_needed, country)
        
        status = ""
        wait_qty = qty_needed - filled
        if wait_qty < 0.001:
            status = "+".join(sources) if sources else "库存异常"
        elif filled > 0:
            status = f"部分缺货(缺{wait_qty:g}):{'+'.join(sources)}"
        else:
            status = f"待下单(需{qty_needed:g})"
            
        snap = inv_mgr.get_sku_snapshot(sku)
        
        res_row = {
            "SKU": sku, "需求标签": tag, "国家": country, "FNSKU": fnsku, "需求数量": qty_needed,
            "订单状态": status, "备注": "; ".join(notes),
            "剩余外协": snap['外协'], "剩余云仓": snap['云仓'], "剩余深仓": snap['深仓'], "剩余PO": snap['PO']
        }
        results.append(res_row)
        
    return pd.DataFrame(results)

# ==========================================
# 5. UI 界面
# ==========================================
col_left, col_right = st.columns([35, 65])

with col_left:
    st.subheader("1. 需求输入")
    col_cfg = {
        "标签列": st.column_config.SelectboxColumn("标签列", options=["新增需求", "当周需求"], required=True),
        "需求数量": st.column_config.NumberColumn("需求数量", required=True, min_value=0),
    }
    sample = pd.DataFrame([{"标签列": "新增需求", "国家": "DE", "SKU": "A001", "FNSKU": "X1", "需求数量": 80}])
    df_input = st.data_editor(sample, column_config=col_cfg, num_rows="dynamic", height=500, use_container_width=True)

with col_right:
    st.subheader("2. 文件上传")
    st.info("💡 提示：库存表必须包含 [SKU, FNSKU, 仓库名称, 可用库存] 列")
    f_inv = st.file_uploader("📂 A. 在库库存表", type=['xlsx', 'xls', 'csv'])
    f_po = st.file_uploader("📂 B. 采购订单追踪表", type=['xlsx', 'xls', 'csv'])
    f_plan = st.file_uploader("📂 C. 提货需求表", type=['xlsx', 'xls', 'csv'])
    
    st.divider()
    
    if st.button("🚀 开始运算", type="primary", use_container_width=True):
        if not (f_inv and f_po and f_plan):
            st.error("❌ 请上传所有3个文件")
        else:
            with st.spinner("读取文件并清洗数据..."):
                # 1. 鲁棒性读取
                df_inv_raw, err1 = load_and_find_header(f_inv, "库存表")
                df_po_raw, err2 = load_and_find_header(f_po, "采购表")
                df_plan_raw, err3 = load_and_find_header(f_plan, "计划表")
                
                if err1 or err2:
                    st.error(f"{err1 or ''} \n {err2 or ''}")
                else:
                    # 2. 映射
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
                    
                    # 3. 初始化并显示自检信息
                    mgr = InventoryManager(df_inv_clean, df_po_clean)
                    
                    # === 数据自检看板 ===
                    st.success("📊 数据读取自检 (如果这里是0，说明表头没对上)")
                    m1, m2, m3 = st.columns(3)
                    m1.metric("读取库存行数", mgr.stats['inv_rows'])
                    m2.metric("识别总库存量", f"{mgr.stats['total_stock']:,.0f}")
                    m3.metric("识别总PO量", f"{mgr.stats['total_po']:,.0f}")
                    
                    if mgr.stats['total_stock'] == 0:
                        st.warning("⚠️ 警告：系统未识别到任何有效库存！请检查库存表的【可用库存】列是否包含数字，或表头是否包含【SKU】。")
                    
                    # 4. 运行
                    final_df = run_full_process(df_input, mgr, df_plan_raw)
                    
                    if final_df.empty:
                        st.warning("无结果")
                    else:
                        st.dataframe(final_df, use_container_width=True)
                        buf = io.BytesIO()
                        with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                            final_df.to_excel(writer, index=False)
                        st.download_button("📥 下载 V7 结果.xlsx", buf.getvalue(), "V7_Allocation.xlsx", "application/vnd.ms-excel")
