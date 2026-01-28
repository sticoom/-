import streamlit as st
import pandas as pd
import io
import copy
import re

# ==========================================
# 1. 基础配置
# ==========================================
st.set_page_config(page_title="智能调拨系统 V19.0 (自动优先级版)", layout="wide", page_icon="🦁")

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
st.title("🦁 智能库存分配 V19.0 (W3优先W4-自动排序)")

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

def normalize_wh_name(name):
    """
    通用仓库名称标准化逻辑
    规则：
    1. 包含'深' -> 深仓
    2. 包含'外协' -> 外协
    3. 包含'云'或'天源' -> 云仓
    4. 包含'PO'或'采购' -> 采购订单
    """
    n = str(name).strip().upper() # 转大写处理
    if "深" in n: return "深仓"
    if "外协" in n: return "外协" # 只要包含外协两个字
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
        for i, row in df.head(20).iterrows():
            row_str = " ".join([str(v).upper() for v in row.values])
            if "SKU" in row_str:
                header_idx = i
                break
        
        if header_idx != -1:
            df.columns = df.iloc[header_idx]
            df = df.iloc[header_idx+1:]
        
        df.columns = [str(c).strip() for c in df.columns]
        df.dropna(how='all', inplace=True)
        return df, None
    except Exception as e:
        return None, f"读取错误: {str(e)}"

def smart_col(df, candidates):
    for c in df.columns:
        if c in candidates: return c
        for cand in candidates:
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
        self.stats = {'total_stock': 0, 'filtered': 0}
        
        self._init_inventory(df_inv)
        self._init_po(df_po)
        self.orig_stock = copy.deepcopy(self.stock)
        self.orig_po = copy.deepcopy(self.po)

    def _init_inventory(self, df):
        if df is None or df.empty: return
        
        c_sku = smart_col(df, ['SKU'])
        c_fnsku = smart_col(df, ['FNSKU'])
        c_wh = smart_col(df, ['仓库名称', '仓库'])
        c_qty = smart_col(df, ['可用库存', '数量'])

        for _, row in df.iterrows():
            w_name = str(row.get(c_wh, ''))
            # 过滤逻辑
            if any(x in w_name.upper() for x in ["沃尔玛", "WALMART", "TEMU"]):
                self.stats['filtered'] += 1
                continue
            
            sku = str(row.get(c_sku, '')).strip()
            if not sku: continue
            
            f_raw = row.get(c_fnsku, '')
            fnsku = str(f_raw).strip() if pd.notna(f_raw) else ""
            qty = clean_number(row.get(c_qty, 0))
            
            if qty <= 0: continue
            
            # 使用统一的标准化逻辑
            w_type = normalize_wh_name(w_name)
            self.stats['total_stock'] += qty
            
            if sku not in self.stock: self.stock[sku] = {}
            if fnsku not in self.stock[sku]: self.stock[sku][fnsku] = {'深仓':0, '外协':0, '云仓':0, '其他':0}
            self.stock[sku][fnsku][w_type] = self.stock[sku][fnsku].get(w_type, 0) + qty

    def _init_po(self, df):
        if df is None or df.empty: return
        
        c_sku = smart_col(df, ['SKU'])
        c_qty = smart_col(df, ['未入库量', '数量'])
        c_req = smart_col(df, ['需求人', '申请人'])
        
        block_list = ["陈丹丹", "张萍", "杨上儒", "陈炜填", "贝少婷", "詹翠萍"]
        
        for _, row in df.iterrows():
            # 黑名单过滤
            if c_req:
                req = str(row.get(c_req, ''))
                if any(b in req for b in block_list): continue
                
            sku = str(row.get(c_sku, '')).strip()
            qty = clean_number(row.get(c_qty, 0))
            
            if sku and qty > 0:
                self.po[sku] = self.po.get(sku, 0) + qty

    def get_snapshot(self, sku):
        res = {'深仓':0, '外协':0, '云仓':0, 'PO': self.po.get(sku, 0)}
        if sku in self.stock:
            for f in self.stock[sku]:
                for w in ['深仓', '外协', '云仓']:
                    res[w] += self.stock[sku][f].get(w, 0)
        return res

    def execute_deduction(self, sku, target_fnsku, qty_needed, strategy_chain):
        """
        核心扣减逻辑
        """
        qty_remain = qty_needed
        breakdown_notes = []
        used_sources = []
        process_details = {'wh': [], 'fnsku': [], 'qty': 0}
        
        for src_type, src_name in strategy_chain:
            if qty_remain <= 0: break
            
            take_total = 0
            
            if src_type == 'stock':
                if sku in self.stock:
                    # A. 优先同 FNSKU
                    if target_fnsku in self.stock[sku]:
                        avail = self.stock[sku][target_fnsku].get(src_name, 0)
                        take = min(avail, qty_remain)
                        if take > 0:
                            self.stock[sku][target_fnsku][src_name] -= take
                            qty_remain -= take
                            take_total += take
                    
                    # B. 加工 (其他 FNSKU)
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
                                
            elif src_type == 'po':
                avail = self.po.get(sku, 0)
                take = min(avail, qty_remain)
                if take > 0:
                    self.po[sku] -= take
                    qty_remain -= take
                    take_total += take
            
            if take_total > 0:
                used_sources.append(src_name)

        return qty_needed - qty_remain, breakdown_notes, used_sources, process_details

    def find_best_single_warehouse(self, sku, target_fnsku, qty_needed, candidates):
        """
        US策略优化：寻找能一次性满足需求的仓库
        """
        for src_type, src_name in candidates:
            total_avail = 0
            if src_type == 'stock' and sku in self.stock:
                for f in self.stock[sku]:
                    total_avail += self.stock[sku][f].get(src_name, 0)
            elif src_type == 'po':
                total_avail = self.po.get(sku, 0)
            
            if total_avail >= qty_needed:
                return (src_type, src_name)
        return None

# ==========================================
# 4. 策略生成器 (含 US 优化)
# ==========================================
def get_strategy(inv_mgr, sku, target_fnsku, qty, country, preferred_status=None):
    """
    生成扣减顺序
    preferred_status: 原始状态 (如 '深圳仓库存')，用于 Base 任务
    """
    is_us = 'US' in str(country).upper()
    
    # 基础优先级池
    pool_non_us = [('stock', '深仓'), ('stock', '云仓'), ('stock', '外协'), ('po', '采购订单')]
    pool_us = [('stock', '外协'), ('stock', '云仓'), ('stock', '深仓'), ('po', '采购订单')]
    
    # 1. 如果有原始状态 (Tier 0)，强制置顶
    final_strategy = []
    base_pool = pool_us if is_us else pool_non_us
    
    if preferred_status:
        # 使用 fuzzy matching 标准化用户输入
        std_status = normalize_wh_name(preferred_status)
        if std_status != "其他":
            target = next((x for x in base_pool if x[1] == std_status), None)
            if target:
                final_strategy.append(target)
                base_pool = [x for x in base_pool if x != target]
    
    # 2. US 整仓优先策略
    if is_us:
        best_single = inv_mgr.find_best_single_warehouse(sku, target_fnsku, qty, base_pool)
        if best_single:
            if best_single in base_pool:
                base_pool.remove(best_single)
                base_pool.insert(0, best_single)
    
    final_strategy.extend(base_pool)
    return final_strategy

# ==========================================
# 5. 主逻辑流程
# ==========================================
def run_allocation(df_input, inv_mgr, df_plan):
    tasks = []
    
    # --- 1. 提货计划 (Tier -1) ---
    if df_plan is not None and not df_plan.empty:
        c_sku = smart_col(df_plan, ['SKU'])
        c_qty = smart_col(df_plan, ['数量', '计划'])
        if c_sku and c_qty:
            for _, row in df_plan.iterrows():
                sku = str(row.get(c_sku, '')).strip()
                qty = clean_number(row.get(c_qty, 0))
                if qty > 0:
                    strat = get_strategy(inv_mgr, sku, "", qty, "Non-US") 
                    inv_mgr.execute_deduction(sku, "", qty, strat)

    # --- 2. 任务拆解 (Tier 0-4) ---
    for idx, row in df_input.iterrows():
        sku = str(row['SKU']).strip()
        fnsku = str(row.get('FNSKU', '')).strip()
        country = str(row['国家']).strip()
        # [修改点] 移除标签列的读取，完全依赖数值列判断优先级
        
        # 数量读取
        w3_orig = clean_number(row.get('第三周发货原始数量', 0))
        w3_final = clean_number(row.get('第三周发货最终数量', 0))
        w3_status = str(row.get('第三周发货原始状态', ''))
        w4_qty = clean_number(row.get('第四周发货原始数量', 0))
        
        is_us = 'US' in country.upper()
        
        # Task A: W3 Base (Tier 0) - 最高优先
        if w3_orig > 0:
            tasks.append({
                'row_idx': idx, 'type': 'w3_base', 'priority': 0,
                'sku': sku, 'fnsku': fnsku, 'country': country, 'qty': w3_orig,
                'pref_status': w3_status 
            })
            
        # Task B: W3 Incr (Tier 1/2) - 次优先
        incr = w3_final - w3_orig
        if incr > 0:
            p = 2 if is_us else 1
            tasks.append({
                'row_idx': idx, 'type': 'w3_incr', 'priority': p,
                'sku': sku, 'fnsku': fnsku, 'country': country, 'qty': incr,
                'pref_status': None
            })
            
        # Task C: W4 Week (Tier 3/4) - 最后分配
        if w4_qty > 0:
            p = 4 if is_us else 3
            tasks.append({
                'row_idx': idx, 'type': 'w4', 'priority': p,
                'sku': sku, 'fnsku': fnsku, 'country': country, 'qty': w4_qty,
                'pref_status': None
            })

    # --- 3. 执行分配 ---
    # 排序保证：W3 Base(0) -> Non-US Incr(1) -> US Incr(2) -> Non-US W4(3) -> US W4(4)
    tasks.sort(key=lambda x: x['priority'])
    
    results = {} 
    
    for t in tasks:
        rid = t['row_idx']
        if rid not in results:
            results[rid] = {
                'w3_final': 0, 'w3_filled': 0, 
                'w4_final': 0, 'w4_filled': 0,
                'w3_src': [], 'w4_src': [],
                'w3_proc': {'wh':[], 'fnsku':[], 'qty':0},
                'w4_proc': {'wh':[], 'fnsku':[], 'qty':0}
            }
            
        strat = get_strategy(inv_mgr, t['sku'], t['fnsku'], t['qty'], t['country'], t['pref_status'])
        filled, notes, srcs, proc = inv_mgr.execute_deduction(t['sku'], t['fnsku'], t['qty'], strat)
        
        # 归档结果
        if 'w3' in t['type']:
            results[rid]['w3_final'] += t['qty']
            results[rid]['w3_filled'] += filled
            results[rid]['w3_src'].extend(srcs)
            results[rid]['w3_proc']['wh'].extend(proc['wh'])
            results[rid]['w3_proc']['fnsku'].extend(proc['fnsku'])
            results[rid]['w3_proc']['qty'] += proc['qty']
        else:
            results[rid]['w4_final'] += t['qty']
            results[rid]['w4_filled'] += filled
            results[rid]['w4_src'].extend(srcs)
            results[rid]['w4_proc']['wh'].extend(proc['wh'])
            results[rid]['w4_proc']['fnsku'].extend(proc['fnsku'])
            results[rid]['w4_proc']['qty'] += proc['qty']

    # --- 4. 构建输出表 ---
    output_rows = []
    for idx, row in df_input.iterrows():
        res = results.get(idx, {
            'w3_final':0, 'w3_filled':0, 'w4_final':0, 'w4_filled':0,
            'w3_src':[], 'w4_src':[], 
            'w3_proc':{'wh':[], 'fnsku':[], 'qty':0},
            'w4_proc':{'wh':[], 'fnsku':[], 'qty':0}
        })
        
        # 基础数据
        sku = str(row['SKU'])
        w3_orig = clean_number(row.get('第三周发货原始数量', 0))
        
        calc_w3_total = res['w3_final']
        calc_w4_total = res['w4_final']
        
        # 状态生成
        w3_status_str = "+".join(sorted(set(res['w3_src']))) if res['w3_src'] else "无"
        w4_status_str = "+".join(sorted(set(res['w4_src']))) if res['w4_src'] else "无"
        
        # 增量来源分析
        orig_stat = str(row.get('第三周发货原始状态', ''))
        norm_orig_stat = normalize_wh_name(orig_stat)
        
        w3_compare_str = f"[原:{orig_stat}]"
        diff_src = []
        for s in res['w3_src']:
             if normalize_wh_name(s) != norm_orig_stat:
                 diff_src.append(s)
                 
        if diff_src:
            w3_compare_str += f" + [增:{'+'.join(set(diff_src))}]"
            
        # 满足度
        shortage = (calc_w3_total + calc_w4_total) - (res['w3_filled'] + res['w4_filled'])
        is_full = "✅ 全满足" if shortage <= 0 else f"❌ 不满足 (缺{to_int(shortage)})"
        
        # 加工信息 W3
        w3_p_fn = ";".join(res['w3_proc']['fnsku'])
        w3_p_wh = ";".join(set(res['w3_proc']['wh']))
        w3_p_qt = to_int(res['w3_proc']['qty']) if res['w3_proc']['qty'] > 0 else ""
        
        # 加工信息 W4
        w4_p_fn = ";".join(res['w4_proc']['fnsku'])
        w4_p_wh = ";".join(set(res['w4_proc']['wh']))
        w4_p_qt = to_int(res['w4_proc']['qty']) if res['w4_proc']['qty'] > 0 else ""
        
        # 剩余库存
        snap = inv_mgr.get_snapshot(sku)
        
        out_row = {
            "国家": row['国家'],
            "SKU": sku,
            "FNSKU": row.get('FNSKU', ''),
            
            # W3 信息
            "第三周发货原始数量": to_int(w3_orig),
            "第三周发货原始状态": orig_stat,
            "第三周发货最终数量": to_int(calc_w3_total),
            "第三周发货最终状态": w3_status_str,
            "第三周需加工FNSKU": w3_p_fn,
            "第三周加工库区": w3_p_wh,
            "第三周加工数量": w3_p_qt,
            
            # W4 信息
            "第四周发货原始数量": to_int(calc_w4_total),
            "第四周发货最终状态": w4_status_str,
            "第四周需加工FNSKU": w4_p_fn,
            "第四周加工库区": w4_p_wh,
            "第四周加工数量": w4_p_qt,
            
            # 对比与统计
            "第三周需求对比(原->新)": f"{to_int(w3_orig)} -> {to_int(calc_w3_total)}",
            "最终发货总数": to_int(res['w3_filled'] + res['w4_filled']),
            "发货对比(原->终)": f"{to_int(min(w3_orig, res['w3_filled']))} -> {to_int(res['w3_filled'] + res['w4_filled'])}",
            "是否全满足": is_full,
            "库存分配状态对比": w3_compare_str,
            
            # 剩余
            "剩_深仓": to_int(snap['深仓']),
            "剩_外协": to_int(snap['外协']),
            "剩_云仓": to_int(snap['云仓']),
            "剩_PO": to_int(snap['PO']),
            
            # 辅助
            "运营": row.get('运营', ''),
            "店铺": row.get('店铺', ''),
            "备注": row.get('备注', '')
        }
        output_rows.append(out_row)

    df_out = pd.DataFrame(output_rows)
    if not df_out.empty:
        df_out.sort_values(by=['SKU', '国家'], inplace=True)
        
    return df_out

# ==========================================
# 6. UI 渲染
# ==========================================
# 初始化 Session State
if 'df_demand' not in st.session_state:
    st.session_state.df_demand = pd.DataFrame([{
        "国家": "US", "SKU": "TEST-001", "FNSKU": "F001",
        "第三周发货原始数量": 50, "第三周发货原始状态": "深圳仓库存",
        "第三周发货最终数量": 80,
        "第四周发货原始数量": 20,
        "运营": "Op1", "店铺": "Shop1", "备注": ""
    }])

col_main, col_side = st.columns([75, 25])

with col_main:
    st.subheader("1. 需求填报 (在线编辑)")
    st.info("💡 请直接在下方表格输入数据，右键可增加行/删除行")
    
    # [修改点] 移除标签列配置，国家列改为自由文本
    col_config = {
        "国家": st.column_config.TextColumn("国家", required=True),
        "SKU": st.column_config.TextColumn("SKU", required=True),
        "第三周发货原始数量": st.column_config.NumberColumn("W3原始数", min_value=0),
        "第三周发货最终数量": st.column_config.NumberColumn("W3最终数", min_value=0),
        "第四周发货原始数量": st.column_config.NumberColumn("W4原始数", min_value=0),
    }
    
    edited_df = st.data_editor(
        st.session_state.df_demand,
        num_rows="dynamic",
        use_container_width=True,
        column_config=col_config,
        height=400
    )
    
    if not edited_df.equals(st.session_state.df_demand):
        st.session_state.df_demand = edited_df

with col_side:
    st.subheader("2. 库存文件")
    f_inv = st.file_uploader("库存表 (必填)", type=['xlsx', 'csv'])
    f_po = st.file_uploader("PO表 (必填)", type=['xlsx', 'csv'])
    f_plan = st.file_uploader("计划表 (选填)", type=['xlsx', 'csv'])
    
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
                    final_df = run_allocation(edited_df, mgr, df_plan_raw)
                    
                    st.success("计算完成!")
                    
                    def highlight(row):
                        return ['background-color: #ffcdd2' if "不满足" in str(row['是否全满足']) else '' for _ in row]
                    
                    st.dataframe(final_df.style.apply(highlight, axis=1), use_container_width=True)
                    
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                        final_df.to_excel(writer, sheet_name='结果', index=False)
                        writer.sheets['结果'].freeze_panes(1, 0)
                    
                    st.download_button("📥 下载结果.xlsx", buf.getvalue(), "V19_Result.xlsx")
        else:
            st.warning("请完善需求表并上传库存文件")
