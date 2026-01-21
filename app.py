import streamlit as st
import pandas as pd
import io

# ==========================================
# 1. 基础配置
# ==========================================
st.set_page_config(page_title="高级智能调拨系统 V6.0", layout="wide", page_icon="🦁")

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
st.title("🦁 智能库存分配 V6.0 (US整单优先+SKU聚合输出)")

# ==========================================
# 2. 核心：库存管理器
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

    def get_sku_snapshot(self, sku):
        """获取某SKU当前各维度的总库存(用于输出表展示剩余量)"""
        res = {'外协': 0, '云仓': 0, '深仓': 0, 'PO': 0}
        
        # 统计现货
        if sku in self.stock:
            for f_key in self.stock[sku]:
                for w_type in ['外协', '云仓', '深仓']:
                    res[w_type] += self.stock[sku][f_key].get(w_type, 0)
        # 统计PO
        res['PO'] = self.po.get(sku, 0)
        return res

    def check_single_source_capacity(self, sku, wh_type):
        """
        检查某仓库(wh_type)针对某SKU的总可用量(含所有FNSKU)
        用于判断是否能整单满足
        """
        total = 0
        if sku in self.stock:
            for f_key in self.stock[sku]:
                total += self.stock[sku][f_key].get(wh_type, 0)
        return total

    def check_po_capacity(self, sku):
        return self.po.get(sku, 0)

    # --- 核心分配算法 ---
    def allocate_strict(self, sku, target_fnsku, qty_needed, strategy_chain):
        """
        params:
            strategy_chain: [('stock', '深仓'), ('stock', '外协'), ('po', '采购订单')]
        return:
            filled_qty, details_list, sources_set
        """
        qty_remain = qty_needed
        breakdown_notes = []
        used_sources = []
        
        for src_type, src_name in strategy_chain:
            if qty_remain <= 0: break
            
            step_taken = 0
            
            if src_type == 'stock':
                # 1. 精确匹配
                if sku in self.stock and target_fnsku in self.stock[sku]:
                    avail = self.stock[sku][target_fnsku].get(src_name, 0)
                    take = min(avail, qty_remain)
                    if take > 0:
                        self.stock[sku][target_fnsku][src_name] -= take
                        qty_remain -= take
                        step_taken += take
                        
                # 2. 加工补足 (同SKU换FNSKU)
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
# 3. 辅助函数
# ==========================================
def smart_col(df, candidates):
    cols = [str(c).strip() for c in df.columns]
    for cand in candidates:
        for c in cols:
            if cand in c: return c
    return None

def get_strategy_us_atomic(inv_mgr, sku, qty_needed):
    """
    US 特供逻辑：
    尽量用单一仓库满足整个需求。
    优先级：外协 -> 云仓 -> 深仓 -> PO
    如果单仓都不满足，则返回降级的混合策略。
    """
    # 1. 检查外协是否能整单满足
    if inv_mgr.check_single_source_capacity(sku, '外协') >= qty_needed:
        return [('stock', '外协')]
    
    # 2. 检查云仓
    if inv_mgr.check_single_source_capacity(sku, '云仓') >= qty_needed:
        return [('stock', '云仓')]

    # 3. 检查深仓
    if inv_mgr.check_single_source_capacity(sku, '深仓') >= qty_needed:
        return [('stock', '深仓')]

    # 4. 检查PO
    if inv_mgr.check_po_capacity(sku) >= qty_needed:
        return [('po', '采购订单')]

    # 5. 都不满足，降级为混合拼接 (外协->云仓->深仓->PO)
    return [('stock', '外协'), ('stock', '云仓'), ('stock', '深仓'), ('po', '采购订单')]

def get_strategy_non_us():
    """非US逻辑：深仓 -> 外协 -> 云仓 -> PO (混合补足)"""
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
    
    # --- 阶段一：提货计划预扣减 (清洗库存) ---
    if df_plan is not None and not df_plan.empty:
        p_sku = smart_col(df_plan, ['SKU', 'sku'])
        p_fnsku = smart_col(df_plan, ['FNSKU', 'FnSKU'])
        p_qty = smart_col(df_plan, ['需求', '计划', '数量'])
        p_country = smart_col(df_plan, ['国家', 'Country']) 
        
        if p_sku and p_qty:
            for _, row in df_plan.iterrows():
                sku = str(row[p_sku]).strip()
                fnsku = str(row[p_fnsku]).strip() if p_fnsku else ""
                try: qty = float(row[p_qty])
                except: qty = 0
                if qty <= 0: continue
                
                cty = str(row[p_country]) if p_country else "Non-US"
                is_us = 'US' in cty.upper() or '美国' in cty
                
                # 计划表扣减也遵循同样的逻辑
                if is_us:
                    strategy = get_strategy_us_atomic(inv_mgr, sku, qty)
                else:
                    strategy = get_strategy_non_us()
                
                inv_mgr.allocate_strict(sku, fnsku, qty, strategy)

    # --- 阶段二：分配需求 ---
    
    # 1. 准备数据
    df = df_demand.copy()
    df['需求数量'] = pd.to_numeric(df['需求数量'], errors='coerce').fillna(0)
    df = df[df['需求数量'] > 0]
    
    # 2. 优先级排序 (确保高优先级先分配)
    def get_calc_rank(row):
        tag = str(row.get('标签列', '')).strip()
        cty = str(row.get('国家', '')).strip().upper()
        # 越小越优先
        base_score = 10 if '新增' in tag else 30
        country_offset = 1 if ('US' in cty or '美国' in cty) else 0
        return base_score + country_offset

    df['calc_rank'] = df.apply(get_calc_rank, axis=1)
    # 先按优先级算，分配库存
    df_calc = df.sort_values(by=['calc_rank', '国家'])
    
    results = []
    
    # 3. 逐行分配
    for idx, row in df_calc.iterrows():
        sku = str(row['SKU']).strip()
        fnsku = str(row['FNSKU']).strip()
        country = str(row['国家']).strip()
        qty_needed = row['需求数量']
        tag = row['标签列']
        
        is_us = 'US' in country.upper() or '美国' in country
        
        # 获取策略
        if is_us:
            # US: 尝试单仓满足，不行则降级
            strategy = get_strategy_us_atomic(inv_mgr, sku, qty_needed)
        else:
            # 非US: 混合瀑布流
            strategy = get_strategy_non_us()
        
        # 执行分配
        filled, notes, sources = inv_mgr.allocate_strict(sku, fnsku, qty_needed, strategy)
        
        # 状态判定
        status = ""
        wait_qty = qty_needed - filled
        
        if wait_qty == 0:
            status = "+".join(sources) if sources else "库存异常"
        elif filled > 0:
            status = f"部分缺货(缺{wait_qty}):{'+'.join(sources)}"
        else:
            status = f"待下单(需{qty_needed})"
            
        # 获取快照
        snap = inv_mgr.get_sku_snapshot(sku)
        
        # 组装结果 (按用户要求的输出顺序)
        res_row = {
            "SKU": sku,
            "标签列": tag,
            "国家": country,
            "FNSKU": fnsku,
            "需求数量": qty_needed,
            "订单状态": status,
            "备注": "; ".join(notes),
            "剩余外协库存": snap['外协'],
            "剩余云仓库存": snap['云仓'],
            "剩余深仓库存": snap['深仓'],
            "剩余PO": snap['PO']
        }
        results.append(res_row)
    
    # 4. 最终输出排序 (按SKU聚合，方便查看)
    df_final = pd.DataFrame(results)
    if not df_final.empty:
        # 自定义排序: SKU -> 标签 -> 国家
        # 注意：这里只是为了好看，库存已经分配完了
        df_final = df_final.sort_values(by=['SKU', '标签列', '国家'])
        
    return df_final

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
    st.info("💡 逻辑：非US混合补足 | US整单优先(外协>云仓>深仓>PO)")
    
    f_inv = st.file_uploader("📂 A. 在库库存表", type=['xlsx', 'xls', 'csv'])
    f_po = st.file_uploader("📂 B. 采购订单追踪表", type=['xlsx', 'xls', 'csv'])
    f_plan = st.file_uploader("📂 C. 提货需求表", type=['xlsx', 'xls', 'csv'])
    
    st.divider()
    
    if st.button("🚀 开始运算", type="primary", use_container_width=True):
        if not (f_inv and f_po and f_plan):
            st.error("❌ 请上传所有3个必要文件！")
        else:
            with st.spinner("正在进行多策略库存分配..."):
                try:
                    df_inv_raw = load_file(f_inv, "库存表")
                    df_po_raw = load_file(f_po, "采购表")
                    df_plan_raw = load_file(f_plan, "提货计划表")
                    
                    if df_inv_raw is not None and df_po_raw is not None:
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
                        final_df = run_full_process(df_input, mgr, df_plan_raw)
                        
                        if final_df.empty:
                            st.warning("⚠️ 结果为空，请检查是否有有效的需求数量。")
                        else:
                            st.success(f"✅ 运算完成！")
                            st.dataframe(final_df, use_container_width=True)
                            
                            buf = io.BytesIO()
                            with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                                final_df.to_excel(writer, index=False)
                            st.download_button("📥 下载结果.xlsx", buf.getvalue(), "V6_Allocation.xlsx", "application/vnd.ms-excel")

                except Exception as e:
                    st.error(f"运行错误: {str(e)}")
