import streamlit as st
import pandas as pd
import io

# ==========================================
# 1. 基础配置
# ==========================================
st.set_page_config(page_title="高级智能调拨系统 V4.0", layout="wide", page_icon="🦁")

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
st.title("🦁 智能库存分配 V4.0 (双重清洗+深度加工逻辑)")

# ==========================================
# 2. 核心：库存管理器 (State Machine)
# ==========================================
class InventoryManager:
    def __init__(self, df_inv, df_po):
        # 结构: self.stock[sku][fnsku][wh_type] = quantity
        self.stock = {}
        # 结构: self.po[sku] = quantity
        self.po = {}
        
        self._init_inventory(df_inv)
        self._init_po(df_po)

    def _get_wh_type(self, wh_name):
        n = str(wh_name).strip()
        if "亚马逊深圳仓" in n or "深仓" in n: return "深仓"
        if "亚马逊外协" in n or "外协" in n: return "外协"
        if "云仓" in n or "天源" in n: return "云仓"
        return "其他"

    def _init_inventory(self, df):
        for _, row in df.iterrows():
            s = str(row.get('SKU', '')).strip()
            f = str(row.get('FNSKU', '')).strip()
            w_name = str(row.get('仓库名称', ''))
            q = pd.to_numeric(row.get('可用库存', 0), errors='coerce') or 0
            
            if q <= 0 or not s: continue
            w_type = self._get_wh_type(w_name)
            
            if s not in self.stock: self.stock[s] = {}
            if f not in self.stock[s]: self.stock[s][f] = {'深仓':0, '外协':0, '云仓':0, '其他':0}
            self.stock[s][f][w_type] = self.stock[s][f].get(w_type, 0) + q

    def _init_po(self, df):
        for _, row in df.iterrows():
            s = str(row.get('SKU', '')).strip()
            q = pd.to_numeric(row.get('未入库量', 0), errors='coerce') or 0
            if q > 0 and s:
                self.po[s] = self.po.get(s, 0) + q

    def get_current_sku_stock(self, sku):
        """获取该SKU当前所有仓库+PO的总库存 (用于展示)"""
        total = 0
        # 加库存
        if sku in self.stock:
            for f_key in self.stock[sku]:
                for w_type in self.stock[sku][f_key]:
                    total += self.stock[sku][f_key][w_type]
        # 加PO
        if sku in self.po:
            total += self.po[sku]
        return total

    # --- 通用分配核心逻辑 (Waterfall) ---
    def allocate_waterfall(self, sku, target_fnsku, qty_needed, strategy_chain):
        """
        核心瀑布流分配函数
        params:
            strategy_chain: list of tuples [('stock', '深仓'), ('stock', '外协'), ('po', '采购订单')]
        return:
            filled_qty: 实际分配的数量
            details_str: 备注明细 (例如: 深仓+外协加工)
            sources_list: 来源列表 (例如: ['深仓', '外协'])
        """
        qty_remain = qty_needed
        breakdown_notes = []
        used_sources = []
        
        for src_type, src_name in strategy_chain:
            if qty_remain <= 0: break
            
            taken_in_this_step = 0
            
            if src_type == 'stock':
                # A. 先扣减精确匹配 (SKU + FNSKU)
                if sku in self.stock and target_fnsku in self.stock[sku]:
                    avail = self.stock[sku][target_fnsku].get(src_name, 0)
                    take = min(avail, qty_remain)
                    if take > 0:
                        self.stock[sku][target_fnsku][src_name] -= take
                        qty_remain -= take
                        taken_in_this_step += take
                
                # B. 如果该仓库还有缺口，扣减加工匹配 (SKU + 其他FNSKU)
                if qty_remain > 0 and sku in self.stock:
                    # 遍历该SKU下该仓库的其他FNSKU
                    for other_f in self.stock[sku]:
                        if other_f == target_fnsku: continue # 跳过自己
                        if qty_remain <= 0: break
                        
                        avail = self.stock[sku][other_f].get(src_name, 0)
                        take = min(avail, qty_remain)
                        if take > 0:
                            self.stock[sku][other_f][src_name] -= take
                            qty_remain -= take
                            taken_in_this_step += take
                            breakdown_notes.append(f"{src_name}加工(用{other_f}补{take})")
            
            elif src_type == 'po':
                # C. 扣减PO (只看SKU)
                if sku in self.po:
                    avail = self.po[sku]
                    take = min(avail, qty_remain)
                    if take > 0:
                        self.po[sku] -= take
                        qty_remain -= take
                        taken_in_this_step += take
            
            if taken_in_this_step > 0:
                if src_name not in used_sources:
                    used_sources.append(src_name)
        
        filled_qty = qty_needed - qty_remain
        details_str = "; ".join(breakdown_notes)
        return filled_qty, details_str, used_sources

# ==========================================
# 3. 辅助函数
# ==========================================
def smart_col(df, candidates):
    cols = [str(c).strip() for c in df.columns]
    for cand in candidates:
        for c in cols:
            if cand in c: return c
    return None

def get_strategy(country_str):
    """根据国家返回仓库优先顺序"""
    c = str(country_str).upper().strip()
    is_us = 'US' in c or '美国' in c
    
    if is_us:
        # US逻辑: 外协 -> 云仓 -> 深仓 -> PO
        return [('stock', '外协'), ('stock', '云仓'), ('stock', '深仓'), ('po', '采购订单')]
    else:
        # 非US逻辑: 深仓 -> 外协 -> 云仓 -> PO
        return [('stock', '深仓'), ('stock', '外协'), ('stock', '云仓'), ('po', '采购订单')]

def load_file(file, type_tag):
    if not file: return None
    try:
        file.seek(0)
        if file.name.endswith('.csv'): df = pd.read_csv(file)
        else: df = pd.read_excel(file)
        return df
    except Exception as e:
        st.error(f"{type_tag} 读取失败: {e}")
        return None

# ==========================================
# 4. 业务逻辑主流程
# ==========================================
def run_full_process(df_demand, inv_mgr, df_plan):
    
    # --- 阶段一：执行提货计划预扣减 (The Plan) ---
    # 逻辑：遍历计划表，根据计划表的国家，执行同样的仓库扣减逻辑
    if df_plan is not None and not df_plan.empty:
        # 识别计划表列名
        p_sku = smart_col(df_plan, ['SKU', 'sku'])
        p_fnsku = smart_col(df_plan, ['FNSKU', 'FnSKU'])
        p_qty = smart_col(df_plan, ['需求', '计划', '数量'])
        p_country = smart_col(df_plan, ['国家', 'Country']) # 假设计划表也有国家，如果没有默认非US
        
        if p_sku and p_qty:
            for _, row in df_plan.iterrows():
                sku = str(row[p_sku]).strip()
                fnsku = str(row[p_fnsku]).strip() if p_fnsku else ""
                try:
                    qty = float(row[p_qty])
                except:
                    qty = 0
                
                if qty <= 0: continue
                
                # 确定计划的扣减策略
                cty = str(row[p_country]) if p_country else "Non-US"
                strategy = get_strategy(cty)
                
                # 执行静默扣减 (不记录结果，只为了减少库存)
                inv_mgr.allocate_waterfall(sku, fnsku, qty, strategy)

    # --- 阶段二：执行需求分配 (The Demand) ---
    
    # 1. 数据清洗与排序
    df = df_demand.copy()
    df['需求数量'] = pd.to_numeric(df['需求数量'], errors='coerce').fillna(0)
    df = df[df['需求数量'] > 0]
    
    # 2. 优先级排序 (Sort Key)
    # 顺序: 新增非US(10) > 新增US(20) > 当周非US(30) > 当周US(40)
    def get_sort_key(row):
        tag = str(row.get('标签列', '')).strip()
        cty = str(row.get('国家', '')).strip().upper()
        
        base_score = 10 if '新增' in tag else 30
        country_offset = 10 if ('US' in cty or '美国' in cty) else 0
        
        return base_score + country_offset

    df['sort_key'] = df.apply(get_sort_key, axis=1)
    df_sorted = df.sort_values(by=['sort_key', '国家']) # 同优先级下按国家排序
    
    results = []
    
    # 3. 逐行分配
    for idx, row in df_sorted.iterrows():
        sku = str(row['SKU']).strip()
        fnsku = str(row['FNSKU']).strip()
        country = str(row['国家']).strip()
        qty_needed = row['需求数量']
        
        # 获取当前剩余库存快照 (仅供参考)
        current_stock_snapshot = inv_mgr.get_current_sku_stock(sku)
        
        # 获取策略
        strategy = get_strategy(country)
        
        # 执行分配
        filled, note_str, sources = inv_mgr.allocate_waterfall(sku, fnsku, qty_needed, strategy)
        
        # 生成状态文本
        status = ""
        wait_qty = qty_needed - filled
        
        if wait_qty == 0:
            status = "+".join(sources) # 完全满足
        elif filled > 0:
            status = f"部分缺货(缺{wait_qty}):{'+'.join(sources)}"
        else:
            status = f"待下单(需{qty_needed})"
            
        # 组装结果行
        res_row = {
            "当前可用库存(SKU总计)": current_stock_snapshot, # 放在第一列方便看
            "标签列": row['标签列'],
            "国家": row['国家'],
            "SKU": sku,
            "FNSKU": fnsku,
            "需求数量": qty_needed,
            "订单状态": status,
            "备注": note_str
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
    sample = pd.DataFrame([
        {"标签列": "新增需求", "国家": "DE", "SKU": "A001", "FNSKU": "X1", "需求数量": 80},
    ])
    df_input = st.data_editor(sample, column_config=col_cfg, num_rows="dynamic", height=500, use_container_width=True)

with col_right:
    st.subheader("2. 引用表格上传")
    st.info("💡 请确保上传文件，系统将先扣减[提货计划]，再分配左侧需求")
    
    f_inv = st.file_uploader("📂 A. 在库库存表 (含: 仓库名称, SKU, FNSKU, 可用库存)", type=['xlsx', 'xls', 'csv'])
    f_po = st.file_uploader("📂 B. 采购订单追踪表 (含: SKU, 未入库量)", type=['xlsx', 'xls', 'csv'])
    f_plan = st.file_uploader("📂 C. 提货需求表 (含: 国家, SKU, FNSKU, 数量)", type=['xlsx', 'xls', 'csv'])
    
    st.divider()
    
    if st.button("🚀 开始运算", type="primary", use_container_width=True):
        if not (f_inv and f_po and f_plan):
            st.error("❌ 必须上传所有3个表格才能进行逻辑运算！")
        else:
            with st.spinner("正在执行: 提货计划预扣减 -> 优先级排序 -> 仓库策略分配..."):
                try:
                    # 读取文件
                    df_inv_raw = load_file(f_inv, "库存表")
                    df_po_raw = load_file(f_po, "采购表")
                    df_plan_raw = load_file(f_plan, "提货计划表")
                    
                    if df_inv_raw is not None and df_po_raw is not None:
                        # 映射标准列名
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
                        
                        # 初始化管理器
                        mgr = InventoryManager(df_inv_clean, df_po_clean)
                        
                        # 运行全流程
                        final_df = run_full_process(df_input, mgr, df_plan_raw)
                        
                        if final_df.empty:
                            st.warning("⚠️ 结果为空，请检查是否有有效的需求数量。")
                        else:
                            st.success(f"✅ 运算完成！已处理 {len(final_df)} 条需求。")
                            st.dataframe(final_df, use_container_width=True)
                            
                            # 导出
                            buf = io.BytesIO()
                            with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                                final_df.to_excel(writer, index=False)
                            st.download_button("📥 下载详细分配结果.xlsx", buf.getvalue(), "V4_Allocation_Result.xlsx", "application/vnd.ms-excel")

                except Exception as e:
                    st.error(f"❌ 程序发生错误: {str(e)}")
                    st.write("请检查上传表格的列名是否包含关键字(SKU, FNSKU, 国家, 数量等)")
