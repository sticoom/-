import streamlit as st
import pandas as pd
import io
import copy

# ==========================================
# 1. 基础配置
# ==========================================
st.set_page_config(page_title="智能调拨系统 V16.0 (深度对比版)", layout="wide", page_icon="🦁")

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
st.title("🦁 智能库存分配 V16.0 (原始占用 + 增量补货 + 状态对比)")

# ==========================================
# 2. 数据清洗与读取
# ==========================================
def clean_number(x):
    """强制清洗为数字，处理逗号、空格"""
    if pd.isna(x): return 0
    s = str(x).strip().replace(',', '').replace(' ', '')
    try: return float(s)
    except: return 0

def to_int(x):
    """安全转换为整数 (四舍五入)"""
    try:
        return int(round(float(x)))
    except:
        return 0

def load_and_find_header(file, type_tag):
    """自动寻找表头 (鲁棒性读取)"""
    if not file: return None, "未上传"
    try:
        file.seek(0)
        if file.name.endswith('.csv'):
            try: df_preview = pd.read_csv(file, header=None, nrows=15, encoding='utf-8-sig')
            except: 
                file.seek(0)
                df_preview = pd.read_csv(file, header=None, nrows=15, encoding='gbk')
        else:
            df_preview = pd.read_excel(file, header=None, nrows=15)
        
        header_idx = -1
        for i, row in df_preview.iterrows():
            row_str = " ".join([str(v).upper() for v in row.values])
            if "SKU" in row_str:
                header_idx = i
                break
        
        if header_idx == -1: return None, f"❌ {type_tag}: 未找到包含'SKU'的表头行"
        
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
        self.stock = {} 
        self.po = {}
        self.orig_stock = {}
        self.orig_po = {}
        
        self.stats = {
            'inv_rows': 0, 'po_rows': 0, 
            'total_stock': 0, 'total_po': 0,
            'filtered_inv': 0, 'filtered_po': 0 
        }
        
        self._init_inventory(df_inv)
        self._init_po(df_po)
        
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
            
            # === V16 过滤逻辑: 沃尔玛 / TEMU ===
            if any(x in w_name.upper() for x in ["沃尔玛", "WALMART", "TEMU"]):
                self.stats['filtered_inv'] += 1
                continue
            
            q = clean_number(row.get('可用库存', 0))
            
            if q <= 0 or not s: continue
            
            w_type = self._get_wh_type(w_name)
            self.stats['total_stock'] += q
            
            if s not in self.stock: self.stock[s] = {}
            if f not in self.stock[s]: self.stock[s][f] = {'深仓':0, '外协':0, '云仓':0, '其他':0}
            self.stock[s][f][w_type] = self.stock[s][f].get(w_type, 0) + q

    def _init_po(self, df):
        self.stats['po_rows'] = len(df)
        col_req = smart_col(df, ['需求人', '申请人', 'Requester', '业务员'])
        
        # === V16 黑名单逻辑 ===
        block_list = ["陈丹丹", "张萍", "杨上儒", "陈炜填", "贝少婷", "詹翠萍"]
        
        for _, row in df.iterrows():
            s = str(row.get('SKU', '')).strip()
            if col_req:
                requester = str(row.get(col_req, ''))
                if any(name in requester for name in block_list):
                    self.stats['filtered_po'] += 1
                    continue

            q = clean_number(row.get('未入库量', 0))
            if q > 0 and s:
                self.po[s] = self.po.get(s, 0) + q
                self.stats['total_po'] += q

    def get_sku_snapshot(self, sku, use_original=False):
        res = {'外协': 0, '云仓': 0, '深仓': 0, 'PO': 0}
        target_stock = self.orig_stock if use_original else self.stock
        target_po = self.orig_po if use_original else self.po
        
        if sku in target_stock:
            for f_key in target_stock[sku]:
                for w_type in ['外协', '云仓', '深仓']:
                    res[w_type] += target_stock[sku][f_key].get(w_type, 0)
        res['PO'] = target_po.get(sku, 0)
        return res

    def execute_deduction(self, sku, target_fnsku, qty_needed, strategy_chain):
        qty_remain = qty_needed
        breakdown_notes = []
        used_sources = []
        process_details = {'wh': [], 'fnsku': [], 'qty': 0}
        
        for src_type, src_name in strategy_chain:
            if qty_remain <= 0: break
            step_taken = 0
            
            if src_type == 'stock':
                # 1. 优先扣减同FNSKU
                if sku in self.stock and target_fnsku in self.stock[sku]:
                    avail = self.stock[sku][target_fnsku].get(src_name, 0)
                    take = min(avail, qty_remain)
                    if take > 0:
                        self.stock[sku][target_fnsku][src_name] -= take
                        qty_remain -= take
                        step_taken += take
                        
                # 2. 同FNSKU不够，扣减其他FNSKU (即加工)
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
                            # 记录加工信息
                            breakdown_notes.append(f"{src_name}(加工)")
                            process_details['wh'].append(src_name)
                            process_details['fnsku'].append(other_f)
                            process_details['qty'] += take
            
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
        return filled_qty, breakdown_notes, used_sources, process_details

# ==========================================
# 4. 逻辑控制 (策略配置)
# ==========================================
def get_strategy_by_country(country_str):
    """
    根据站点决定仓库扣减优先级 (V15/16 逻辑保持一致)
    """
    c = str(country_str).upper().strip()
    is_us = 'US' in c or '美国' in c
    
    if is_us:
        # US: 外协 > 云仓 > 深仓 > PO
        return [
            ('stock', '外协'), 
            ('stock', '云仓'), 
            ('stock', '深仓'), 
            ('po', '采购订单')
        ], True
    else:
        # 非US: 深仓 > 云仓 > 外协 > PO
        return [
            ('stock', '深仓'), 
            ('stock', '云仓'), 
            ('stock', '外协'), 
            ('po', '采购订单')
        ], False

def run_full_process(df_demand, inv_mgr, df_plan):
    plan_summary_dict = {} 
    
    # ------------------------------------------------
    # 1. 提货计划预扣减 (Priority Top)
    # ------------------------------------------------
    if df_plan is not None and not df_plan.empty:
        p_sku = smart_col(df_plan, ['SKU', 'sku'])
        p_fnsku = smart_col(df_plan, ['FNSKU', 'FnSKU'])
        p_qty = smart_col(df_plan, ['需求', '计划', '数量'])
        p_cty = smart_col(df_plan, ['国家', 'Country'])
        
        if p_sku and p_qty:
            for _, row in df_plan.iterrows():
                sku = str(row[p_sku]).strip()
                f_raw = row[p_fnsku] if p_fnsku else ""
                fnsku = str(f_raw).strip() if pd.notna(f_raw) else ""
                qty = clean_number(row[p_qty])
                cty = str(row[p_cty]) if p_cty else "Non-US"
                
                if qty <= 0: continue
                plan_summary_dict[sku] = plan_summary_dict.get(sku, 0) + qty
                
                strat, _ = get_strategy_by_country(cty)
                inv_mgr.execute_deduction(sku, fnsku, qty, strat)

    # ------------------------------------------------
    # 2. 需求拆解与任务生成
    # ------------------------------------------------
    df = df_demand.copy()
    
    # 映射关键列
    col_sku = smart_col(df, ['SKU', 'sku'])
    col_fnsku = smart_col(df, ['FNSKU', 'FnSKU'])
    col_tag = smart_col(df, ['标签列', '标签'])
    col_country = smart_col(df, ['国家', 'Country'])
    col_qty_new = smart_col(df, ['最新需求', '数量', 'Qty', '需求数量']) # 用户输入的主数量列
    col_qty_old = smart_col(df, ['原始需求', '原始数量', 'OldQty']) # 可选
    
    # 辅助信息列
    col_op = smart_col(df, ['运营', 'Operator'])
    col_shop = smart_col(df, ['店铺', 'Shop'])
    col_remark = smart_col(df, ['备注', 'Remark'])
    
    if not (col_sku and col_tag and col_country and col_qty_new):
        return pd.DataFrame(), pd.DataFrame() 

    allocation_tasks = []

    for idx, row in df.iterrows():
        sku = str(row[col_sku]).strip()
        f_raw = row.get(col_fnsku, '')
        fnsku = str(f_raw).strip() if pd.notna(f_raw) else ""
        tag = str(row.get(col_tag, '')).strip()
        country = str(row.get(col_country, '')).strip()
        
        q_new = clean_number(row.get(col_qty_new, 0))
        q_old = clean_number(row.get(col_qty_old, 0)) if col_qty_old else 0
        
        is_us = 'US' in country.upper() or '美国' in country.upper()
        
        # 确定优先级和分配类型
        if '新增' in tag:
            # === 新增需求 (Base + Incr) ===
            # T0: Base (原始占用)
            if q_old > 0:
                allocation_tasks.append({
                    'row_idx': idx, 'sku': sku, 'fnsku': fnsku, 'country': country,
                    'qty': q_old, 'type': 'base', 'priority': 0
                })
            
            # T1/T2: Incr (增量补货)
            diff = q_new - q_old
            if diff > 0:
                p_score = 2 if is_us else 1
                allocation_tasks.append({
                    'row_idx': idx, 'sku': sku, 'fnsku': fnsku, 'country': country,
                    'qty': diff, 'type': 'incr', 'priority': p_score
                })
        else:
            # === 当周需求/其他 ===
            # T3/T4: Full Qty
            if q_new > 0:
                p_score = 4 if is_us else 3
                allocation_tasks.append({
                    'row_idx': idx, 'sku': sku, 'fnsku': fnsku, 'country': country,
                    'qty': q_new, 'type': 'week', 'priority': p_score
                })

    # 按优先级排序执行 (Base -> Non-US Incr -> US Incr -> Non-US Week -> US Week)
    allocation_tasks.sort(key=lambda x: x['priority'])
    
    # ------------------------------------------------
    # 3. 执行分配
    # ------------------------------------------------
    results_map = {}
    
    for task in allocation_tasks:
        idx = task['row_idx']
        qty = task['qty']
        sku = task['sku']
        fnsku = task['fnsku']
        country = task['country']
        task_type = task['type']
        
        if idx not in results_map:
            results_map[idx] = {
                'qty_base': 0, 'qty_incr': 0, 'qty_week': 0,
                'fill_base': 0, 'fill_incr': 0, 'fill_week': 0,
                'src_base': [], 'src_incr': [], 'src_week': [],
                'proc_wh': [], 'proc_fnsku': [], 'proc_qty': 0
            }
        
        strat, _ = get_strategy_by_country(country)
        filled, notes, sources, proc = inv_mgr.execute_deduction(sku, fnsku, qty, strat)
        
        # 记录分项数据
        if task_type == 'base':
            results_map[idx]['qty_base'] += qty
            results_map[idx]['fill_base'] += filled
            results_map[idx]['src_base'].extend(sources)
        elif task_type == 'incr':
            results_map[idx]['qty_incr'] += qty
            results_map[idx]['fill_incr'] += filled
            results_map[idx]['src_incr'].extend(sources)
        else:
            results_map[idx]['qty_week'] += qty
            results_map[idx]['fill_week'] += filled
            results_map[idx]['src_week'].extend(sources)
            
        # 记录加工
        results_map[idx]['proc_wh'].extend(proc['wh'])
        results_map[idx]['proc_fnsku'].extend(proc['fnsku'])
        results_map[idx]['proc_qty'] += proc['qty']

    # ------------------------------------------------
    # 4. 汇总并构建输出
    # ------------------------------------------------
    processed_rows = []
    verify_data = {}
    
    for idx, row in df.iterrows():
        # 基础列
        res_row = {
            "SKU": str(row[col_sku]).strip(),
            "FNSKU": str(row.get(col_fnsku, '')).strip(),
            "国家": str(row[col_country]).strip(),
            "标签": str(row[col_tag]).strip(),
            "运营": str(row.get(col_op, '')),
            "店铺": str(row.get(col_shop, '')),
            "备注": str(row.get(col_remark, ''))
        }
        
        sku = res_row['SKU']
        q_new = clean_number(row.get(col_qty_new, 0))
        q_old = clean_number(row.get(col_qty_old, 0)) if col_qty_old else 0
        
        if idx in results_map:
            data = results_map[idx]
            
            # 汇总数据
            total_need = data['qty_base'] + data['qty_incr'] + data['qty_week'] # 理论上等于 q_new
            total_fill = data['fill_base'] + data['fill_incr'] + data['fill_week']
            
            # 如果是纯当周需求，q_old 可能为 0，total_need = q_new
            # 如果是新增需求，total_need = q_old + (q_new - q_old) = q_new
            
            shortage = total_need - total_fill
            
            # --- 构建对比列 ---
            # 1. 需求对比
            demand_compare = f"{to_int(q_old)} -> {to_int(q_new)}"
            
            # 2. 发货对比
            # 原始部分发货了多少? data['fill_base']
            # 最终总发货多少? total_fill
            fill_compare = f"{to_int(data['fill_base'])} -> {to_int(total_fill)}"
            
            # 3. 状态对比 (Alloc Status)
            # 格式: [原始: 深仓] -> [新增: 外协]
            base_src = "+".join(set(data['src_base'])) if data['src_base'] else "无"
            incr_src = "+".join(set(data['src_incr'])) if data['src_incr'] else "无"
            week_src = "+".join(set(data['src_week'])) if data['src_week'] else "无"
            
            if '新增' in res_row['标签']:
                status_compare = f"[原:{base_src}] + [增:{incr_src}]"
            else:
                status_compare = f"[当周:{week_src}]"
                
            # 4. 是否全满足
            if shortage <= 0.001:
                is_satisfied = "✅ 全满足"
            else:
                is_satisfied = f"❌ 不满足 (缺{to_int(shortage)})"
                
            # 加工信息
            p_fn = ";".join(data['proc_fnsku'])
            p_qt = to_int(data['proc_qty']) if data['proc_qty'] > 0 else ""
            
            res_row.update({
                "需求对比(原->新)": demand_compare,
                "最终发货数量": to_int(total_fill),
                "发货对比(原->终)": fill_compare,
                "库存分配状态对比": status_compare,
                "是否全满足": is_satisfied,
                "加工FNSKU": p_fn,
                "加工数量": p_qt
            })
            
            # 统计验证
            curr = inv_mgr.get_sku_snapshot(sku)
            res_row.update({
                "剩_深仓": to_int(curr['深仓']),
                "剩_外协": to_int(curr['外协']),
                "剩_云仓": to_int(curr['云仓']),
                "剩_PO": to_int(curr['PO'])
            })
            
        else:
            # 没分配 (可能需求为0)
            res_row.update({
                "需求对比(原->新)": f"{to_int(q_old)} -> {to_int(q_new)}",
                "最终发货数量": 0,
                "发货对比(原->终)": "0 -> 0",
                "库存分配状态对比": "-",
                "是否全满足": "-",
                "加工FNSKU": "", "加工数量": "",
                "剩_深仓": 0, "剩_外协": 0, "剩_云仓": 0, "剩_PO": 0
            })

        processed_rows.append(res_row)

    # ------------------------------------------------
    # 5. 最终排序与展示
    # ------------------------------------------------
    df_res = pd.DataFrame(processed_rows)
    
    if not df_res.empty:
        # 辅助排序列
        def get_sort_key(row):
            tag = row['标签']
            cty = row['国家'].upper()
            is_us = 'US' in cty or '美国' in cty
            
            # 顺序: 新增(0) > 当周(10)
            score_tag = 0 if '新增' in tag else 10
            # 顺序: 非US(0) > US(1)
            score_cty = 1 if is_us else 0
            
            return score_tag + score_cty

        df_res['sort_key'] = df_res.apply(get_sort_key, axis=1)
        
        # 核心排序: SKU -> 优先级
        df_res = df_res.sort_values(by=['SKU', 'sort_key'])
        df_res = df_res.drop(columns=['sort_key'])
        
        # 列顺序微调
        cols_order = [
            "SKU", "FNSKU", "国家", "标签", "需求对比(原->新)", "最终发货数量",
            "是否全满足", "库存分配状态对比", "加工FNSKU", "加工数量",
            "剩_深仓", "剩_外协", "剩_云仓", "剩_PO",
            "运营", "店铺", "备注"
        ]
        # 补齐其他列
        final_cols = [c for c in cols_order if c in df_res.columns] + [c for c in df_res.columns if c not in cols_order]
        df_res = df_res[final_cols]
        
    return df_res, pd.DataFrame() # 简化，不再输出验证表

# ==========================================
# 6. UI 界面
# ==========================================
col_left, col_right = st.columns([30, 70])

with col_left:
    st.subheader("1. 需求输入")
    st.info("💡 必须包含：标签、国家、SKU、FNSKU、数量 (对应最新需求)")
    st.markdown("若需计算增量，请确保Excel包含 **原始需求** 列")
    
    tab1, tab2 = st.tabs(["手动录入", "文件上传"])
    df_input = None
    
    with tab1:
        # 手动录入示例
        sample = pd.DataFrame([{
            "标签": "新增需求", "国家": "DE", "SKU": "A001", "FNSKU": "X1", 
            "原始需求": 50, "数量": 80, "运营": "Op1", "店铺": "S1", "备注": ""
        }])
        df_manual = st.data_editor(sample, num_rows="dynamic", use_container_width=True)
        if not df_manual.empty: df_input = df_manual
        
    with tab2:
        up_file = st.file_uploader("📤 上传需求表格", type=['xlsx', 'xls', 'csv'])
        if up_file:
            df_input, _ = load_and_find_header(up_file, "需求表")
            if df_input is not None:
                st.success(f"已加载 {len(df_input)} 行数据")

with col_right:
    st.subheader("2. 库存文件上传")
    st.warning("⚠️ 沃尔玛/TEMU 仓库将被自动过滤 | 指定黑名单人员PO将被过滤")
    
    c1, c2, c3 = st.columns(3)
    f_inv = c1.file_uploader("📂 A. 库存表 (必填)", type=['xlsx', 'csv'])
    f_po = c2.file_uploader("📂 B. PO表 (必填)", type=['xlsx', 'csv'])
    f_plan = c3.file_uploader("📂 C. 计划表 (选填)", type=['xlsx', 'csv'])
    
    st.divider()
    
    if st.button("🚀 开始运算 (V16.0)", type="primary", use_container_width=True):
        if not (f_inv and f_po):
            st.error("❌ 缺少库存或PO表")
        elif df_input is None or df_input.empty:
            st.error("❌ 缺少需求数据")
        else:
            with st.spinner("正在进行多维度分配与对比计算..."):
                try:
                    df_inv_raw, _ = load_and_find_header(f_inv, "库存")
                    df_po_raw, _ = load_and_find_header(f_po, "PO")
                    df_plan_raw = None
                    if f_plan: df_plan_raw, _ = load_and_find_header(f_plan, "计划")
                    
                    # 映射列名
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
                    
                    mgr = InventoryManager(df_inv_raw.rename(columns=inv_map), df_po_raw.rename(columns=po_map))
                    
                    final_df, _ = run_full_process(df_input, mgr, df_plan_raw)
                    
                    if final_df.empty:
                        st.warning("结果为空")
                    else:
                        # 样式高亮
                        def highlight_row(row):
                            if "不满足" in str(row['是否全满足']):
                                return ['background-color: #ffebee'] * len(row)
                            return [''] * len(row)

                        st.write("### 分配结果明细")
                        st.dataframe(final_df.style.apply(highlight_row, axis=1), use_container_width=True)
                        
                        buf = io.BytesIO()
                        with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                            final_df.to_excel(writer, sheet_name='分配结果', index=False)
                            writer.sheets['分配结果'].freeze_panes(1, 0)
                        
                        st.download_button("📥 下载 V16结果.xlsx", buf.getvalue(), "V16_Result.xlsx")
                        
                except Exception as e:
                    st.error(f"Error: {e}")
