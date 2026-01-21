import streamlit as st
import pandas as pd
import io

# ==========================================
# 1. 基础配置
# ==========================================
st.set_page_config(page_title="高级智能调拨系统 V3.0", layout="wide", page_icon="🧩")

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
st.title("🧩 智能库存分配 V3.0 (混合扣减策略)")

# ==========================================
# 2. 核心：库存管理器 (支持部分取货)
# ==========================================
class InventoryManager:
    def __init__(self, df_inv, df_po):
        # stock[sku][fnsku][wh_type] = quantity
        self.stock = {}
        # po[sku] = quantity
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

    def deduct_pre_plan(self, df_plan):
        """预扣减提货计划"""
        for _, row in df_plan.iterrows():
            s = str(row.get('SKU', '')).strip()
            f = str(row.get('FNSKU', '')).strip()
            q = pd.to_numeric(row.get('发货计划', 0), errors='coerce') or 0
            
            if q <= 0: continue
            remaining = q
            
            # 扣库存
            if s in self.stock and f in self.stock[s]:
                for w in ['外协', '云仓', '深仓', '其他']:
                    if remaining <= 0: break
                    avail = self.stock[s][f].get(w, 0)
                    take = min(avail, remaining)
                    self.stock[s][f][w] -= take
                    remaining -= take
            # 扣PO
            if remaining > 0 and s in self.po:
                take = min(self.po[s], remaining)
                self.po[s] -= take

    def take_stock_exact(self, sku, fnsku, wh_type, qty_limit):
        """尝试拿取精确库存 (SKU+FNSKU)"""
        if sku not in self.stock or fnsku not in self.stock[sku]:
            return 0
        
        avail = self.stock[sku][fnsku].get(wh_type, 0)
        take = min(avail, qty_limit)
        
        if take > 0:
            self.stock[sku][fnsku][wh_type] -= take
        return take

    def take_stock_substitute(self, sku, target_fnsku, wh_type, qty_limit):
        """尝试拿取替代库存 (同SKU不同FNSKU, 即加工)"""
        if sku not in self.stock: return 0, []
        
        taken_total = 0
        details = []
        remaining = qty_limit
        
        # 遍历该SKU下其他FNSKU
        for f_key in self.stock[sku]:
            if f_key == target_fnsku: continue # 跳过自己
            if remaining <= 0: break
            
            avail = self.stock[sku][f_key].get(wh_type, 0)
            take = min(avail, remaining)
            
            if take > 0:
                self.stock[sku][f_key][wh_type] -= take
                taken_total += take
                remaining -= take
                details.append(f"{f_key}补{take}")
                
        return taken_total, details

    def take_po(self, sku, qty_limit):
        """尝试拿取PO"""
        if sku not in self.po: return 0
        avail = self.po[sku]
        take = min(avail, qty_limit)
        if take > 0:
            self.po[sku] -= take
        return take

# ==========================================
# 3. 辅助函数
# ==========================================
def smart_col(df, candidates):
    cols = [str(c).strip() for c in df.columns]
    for cand in candidates:
        for c in cols:
            if cand in c: return c
    return None

def load_file(file, type_tag):
    if not file: return None
    try:
        file.seek(0)
        if file.name.endswith('.csv'): df = pd.read_csv(file)
        else: df = pd.read_excel(file)
        return df
    except Exception as e:
        # 这里会捕获缺库错误并提示
        st.error(f"{type_tag} 读取失败: {e}")
        return None

# ==========================================
# 4. 主逻辑：贪婪混合分配
# ==========================================
def run_allocation_logic(df_input, inv_mgr):
    # 1. 过滤空值
    df = df_input.copy()
    df['需求数量'] = pd.to_numeric(df['需求数量'], errors='coerce').fillna(0)
    df = df[df['需求数量'] > 0]
    
    # 2. 排序 (新增 > 当周, 非US > US)
    def get_score(row):
        tag = str(row.get('标签列', '')).strip()
        cty = str(row.get('国家', '')).strip().upper()
        base = 10 if '新增' in tag else 30
        offset = 10 if ('US' in cty or '美国' in cty) else 0
        return base + offset

    df['p_score'] = df.apply(get_score, axis=1)
    df_sorted = df.sort_values(by=['p_score', '国家'])
    
    results = []
    
    # 3. 逐行执行混合扣减
    for idx, row in df_sorted.iterrows():
        sku = str(row['SKU']).strip()
        fnsku = str(row['FNSKU']).strip()
        country = str(row['国家']).strip()
        qty_needed = row['需求数量']
        
        is_us = 'US' in country.upper() or '美国' in country
        
        # 定义扣减顺序 (策略链)
        if not is_us:
            # 非US: 深仓 -> 外协 -> PO
            strategy_chain = [('stock', '深仓'), ('stock', '外协'), ('po', '采购订单')]
        else:
            # US: 外协 -> 云仓 -> PO
            strategy_chain = [('stock', '外协'), ('stock', '云仓'), ('po', '采购订单')]
            
        used_sources = []
        remark_list = []
        qty_remain = qty_needed
        
        # --- 核心 Waterfall 循环 ---
        for src_type, src_name in strategy_chain:
            if qty_remain <= 0: break
            
            taken = 0
            if src_type == 'stock':
                t_exact = inv_mgr.take_stock_exact(sku, fnsku, src_name, qty_remain)
                if t_exact > 0:
                    taken += t_exact
                    qty_remain -= t_exact
                    
                if qty_remain > 0:
                    t_sub, sub_details = inv_mgr.take_stock_substitute(sku, fnsku, src_name, qty_remain)
                    if t_sub > 0:
                        taken += t_sub
                        qty_remain -= t_sub
                        remark_list.append(f"{src_name}加工: " + ",".join(sub_details))
            
            elif src_type == 'po':
                t_po = inv_mgr.take_po(sku, qty_remain)
                if t_po > 0:
                    taken += t_po
                    qty_remain -= t_po
            
            if taken > 0:
                used_sources.append(src_name)
        
        # 结果判定
        final_status = ""
        if qty_remain == 0:
            final_status = "+".join(used_sources) 
        elif qty_remain < qty_needed:
            final_status = "待下单(部分缺货)"
            remark_list.append(f"总需{qty_needed}, 缺{qty_remain}, 已配:{'+'.join(used_sources)}")
        else:
            final_status = "待下单"
            
        res_row = row.drop('p_score').to_dict()
        res_row['订单状态'] = final_status
        res_row['备注'] = "; ".join(remark_list)
        results.append(res_row)

    return pd.DataFrame(results)

# ==========================================
# 5. UI 界面布局
# ==========================================
col_left, col_right = st.columns([35, 65])

with col_left:
    st.subheader("1. 需求输入")
    col_cfg = {
        "标签列": st.column_config.SelectboxColumn("标签列", options=["新增需求", "当周需求"], required=True),
        "需求数量": st.column_config.NumberColumn("需求数量", required=True, min_value=0),
    }
    init_df = pd.DataFrame(columns=["标签列", "国家", "SKU", "FNSKU", "需求数量"])
    sample = pd.DataFrame([
        {"标签列": "新增需求", "国家": "DE", "SKU": "A001", "FNSKU": "X1", "需求数量": 80},
    ])
    
    df_input = st.data_editor(
        sample, 
        column_config=col_cfg, 
        num_rows="dynamic", 
        height=500, 
        use_container_width=True
    )

with col_right:
    st.subheader("2. 引用表格上传")
    f_inv = st.file_uploader("📂 A. 在库库存表", type=['xlsx', 'xls', 'csv'])
    f_po = st.file_uploader("📂 B. 采购订单追踪表", type=['xlsx', 'xls', 'csv'])
    f_plan = st.file_uploader("📂 C. 提货需求表", type=['xlsx', 'xls', 'csv'])
    
    st.divider()
    
    if st.button("🚀 开始运算", type="primary", use_container_width=True):
        if not (f_inv and f_po and f_plan):
            st.error("请上传所有3个必要文件！")
        else:
            with st.spinner("正在初始化库存池并执行混合分配..."):
                try:
                    df_inv_raw = load_file(f_inv, "库存表")
                    df_po_raw = load_file(f_po, "采购表")
                    df_plan_raw = load_file(f_plan, "提货计划表")
                    
                    if df_inv_raw is not None and df_po_raw is not None and df_plan_raw is not None:
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
                        plan_map = {
                            smart_col(df_plan_raw, ['SKU', 'sku']): 'SKU',
                            smart_col(df_plan_raw, ['FNSKU', 'FnSKU']): 'FNSKU',
                            smart_col(df_plan_raw, ['发货计划', '计划']): '发货计划'
                        }
                        
                        df_inv_clean = df_inv_raw.rename(columns=inv_map)
                        df_po_clean = df_po_raw.rename(columns=po_map)
                        df_plan_clean = df_plan_raw.rename(columns=plan_map)
                        
                        mgr = InventoryManager(df_inv_clean, df_po_clean)
                        mgr.deduct_pre_plan(df_plan_clean)
                        
                        res = run_allocation_logic(df_input, mgr)
                        
                        if res.empty:
                            st.warning("结果为空，请检查是否有有效的需求数量。")
                        else:
                            st.success("✅ 运算完成！")
                            st.dataframe(res, use_container_width=True)
                            
                            buf = io.BytesIO()
                            with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                                res.to_excel(writer, index=False)
                            st.download_button("📥 下载结果.xlsx", buf.getvalue(), "智能分配结果_V3.xlsx", "application/vnd.ms-excel")
                except Exception as e:
                    st.error(f"运行错误: {str(e)}")
