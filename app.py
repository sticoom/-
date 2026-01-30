import streamlit as st
import pandas as pd
import io
import copy

# ==========================================
# 1. 基础配置
# ==========================================
st.set_page_config(page_title="库存智能调拨系统", layout="wide", page_icon="🦁")

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
st.title("🦁 智能库存分配系统")

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
    if "PO" in n or "采购" in n: return "采购订单"
    return "其他"

def load_and_find_header(file, type_tag):
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
        # 扩大搜索范围到前50行
        for i, row in df.head(50).iterrows():
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
    def __init__(self, df_inv, df_po):
        # stock[sku][fnsku][wh_type] = List[Dict]
        self.stock = {} 
        # po[sku][fnsku] = List[Dict]
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
                'qty': qty, 'raw_name': w_name_raw, 'zone': zone
            })

    def _init_po(self, df):
        if df is None or df.empty: return
        
        c_sku = next((c for c in df.columns if 'SKU' in c.upper()), None)
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
            f_raw = row.get(c_fnsku, '') if c_fnsku else ''
            fnsku = str(f_raw).strip() if pd.notna(f_raw) else ""
            
            if sku and qty > 0:
                if sku not in self.po: self.po[sku] = {}
                if fnsku not in self.po[sku]: self.po[sku][fnsku] = []
                
                self.po[sku][fnsku].append({
                    'qty': qty, 'raw_name': '采购订单', 'zone': '-'
                })

    def get_snapshot(self, sku):
        res = {'深仓':0, '外协':0, '云仓':0, '采购订单': 0}
        if sku in self.stock:
            for f in self.stock[sku]:
                for w_type in ['深仓', '外协', '云仓']:
                    res[w_type] += sum(item['qty'] for item in self.stock[sku][f].get(w_type, []))
        if sku in self.po:
            for f in self.po[sku]:
                res['采购订单'] += sum(item['qty'] for item in self.po[sku][f])
        return res

    def execute_deduction(self, sku, target_fnsku, qty_needed, strategy_chain, mode='strict_only'):
        """
        核心扣减逻辑
        """
        qty_remain = qty_needed
        breakdown_notes = []
        used_sources = []
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

            # --- PO 处理 ---
            elif src_type == 'po' and sku in self.po:
                if mode in ['po_any', 'strict_only']:
                    targets = []
                    if mode == 'strict_only':
                        if target_fnsku in self.po[sku]: targets = [target_fnsku]
                    else:
                        targets = list(self.po[sku].keys())
                        
                    for f in targets:
                        if qty_remain <= 0: break
                        items = self.po[sku][f]
                        for item in items:
                            if qty_remain <= 0: break
                            avail = item['qty']
                            if avail <= 0: continue
                            take = min(avail, qty_remain)
                            item['qty'] -= take
                            qty_remain -= take
                            step_taken += take
                            tag = "PO精准" if mode == 'strict_only' else "PO任意"
                            deduction_log.append(f"{tag}(-{to_int(take)})")

                if mode == 'process_only' and qty_remain > 0:
                    for other_f in self.po[sku]:
                        if other_f == target_fnsku: continue
                        if qty_remain <= 0: break
                        items = self.po[sku][other_f]
                        for item in items:
                            if qty_remain <= 0: break
                            avail = item['qty']
                            if avail <= 0: continue
                            take = min(avail, qty_remain)
                            item['qty'] -= take
                            qty_remain -= take
                            step_taken += take
                            breakdown_notes.append(f"PO(加工)")
                            process_details['raw_wh'].append('采购订单')
                            process_details['zone'].append('-')
                            process_details['fnsku'].append(other_f)
                            process_details['qty'] += take
                            deduction_log.append(f"PO加工(-{to_int(take)})")
            
            if step_taken > 0:
                usage_breakdown[src_name] = usage_breakdown.get(src_name, 0) + step_taken
                used_sources.append(src_name)

        return qty_remain, usage_breakdown, process_details, deduction_log

# ==========================================
# 4. 主逻辑流程 (新增：Phase 0 提货计划)
# ==========================================
def run_allocation(df_input, inv_mgr, df_plan, mapping):
    
    # === Phase 0: 提货计划清算 (单独表输出) ===
    plan_results = []
    
    if df_plan is not None and not df_plan.empty:
        c_sku = next((c for c in df_plan.columns if 'SKU' in c.upper()), None)
        # 订单需求 or 数量
        c_qty = next((c for c in df_plan.columns if any(k in str(c) for k in ['需求', '数量', 'Qty'])), None)
        c_country = next((c for c in df_plan.columns if '国家' in c), None)
        c_fnsku = next((c for c in df_plan.columns if 'FNSKU' in c.upper()), None)
        
        if c_sku and c_qty:
            for _, row in df_plan.iterrows():
                sku = str(row.get(c_sku, '')).strip()
                if not sku: continue
                
                f_raw = row.get(c_fnsku, '') if c_fnsku else ''
                fnsku = str(f_raw).strip() if pd.notna(f_raw) else ""
                qty = clean_number(row.get(c_qty, 0))
                cty = str(row.get(c_country, 'Non-US')).strip()
                
                if qty <= 0: continue
                
                snap = inv_mgr.get_snapshot(sku)
                is_us_plan = 'US' in cty.upper() or '美国' in cty
                
                # 初始化结果记录
                p_filled = 0
                p_logs = []
                p_proc = {'raw_wh': [], 'zone': [], 'fnsku': [], 'qty': 0}
                p_usage = {}
                
                if is_us_plan:
                    # US Plan: 外>云>深 (无PO), Strict -> Process
                    strat_plan_us = [('stock', '外协'), ('stock', '云仓'), ('stock', '深仓')]
                    
                    # R1: Strict
                    rem, u1, pr1, l1 = inv_mgr.execute_deduction(sku, fnsku, qty, strat_plan_us, 'strict_only')
                    p_filled += (qty - rem)
                    p_logs.extend(l1)
                    # Merge data (simplified for brevity)
                    for k,v in u1.items(): p_usage[k] = p_usage.get(k, 0) + v
                    
                    # R2: Process (if needed)
                    if rem > 0:
                        rem2, u2, pr2, l2 = inv_mgr.execute_deduction(sku, fnsku, rem, strat_plan_us, 'process_only')
                        p_filled += (rem - rem2)
                        p_logs.extend(l2)
                        for k,v in u2.items(): p_usage[k] = p_usage.get(k, 0) + v
                        p_proc['raw_wh'].extend(pr2['raw_wh'])
                        p_proc['zone'].extend(pr2['zone'])
                        p_proc['fnsku'].extend(pr2['fnsku'])
                        p_proc['qty'] += pr2['qty']
                
                else:
                    # Non-US Plan: 仅深仓 (无PO), Strict -> Process
                    strat_plan_non_us = [('stock', '深仓')]
                    
                    # R1: Strict
                    rem, u1, pr1, l1 = inv_mgr.execute_deduction(sku, fnsku, qty, strat_plan_non_us, 'strict_only')
                    p_filled += (qty - rem)
                    p_logs.extend(l1)
                    for k,v in u1.items(): p_usage[k] = p_usage.get(k, 0) + v
                    
                    # R2: Process
                    if rem > 0:
                        rem2, u2, pr2, l2 = inv_mgr.execute_deduction(sku, fnsku, rem, strat_plan_non_us, 'process_only')
                        p_filled += (rem - rem2)
                        p_logs.extend(l2)
                        for k,v in u2.items(): p_usage[k] = p_usage.get(k, 0) + v
                        p_proc['raw_wh'].extend(pr2['raw_wh'])
                        p_proc['zone'].extend(pr2['zone'])
                        p_proc['fnsku'].extend(pr2['fnsku'])
                        p_proc['qty'] += pr2['qty']

                # 生成计划结果行
                status_parts = []
                for k, v in p_usage.items():
                    if v > 0: status_parts.append(f"{k}{to_int(v)}")
                status_str = "+".join(status_parts) if status_parts else "库存不足"
                
                if p_filled < qty:
                    status_str += f"(缺{to_int(qty - p_filled)})"

                plan_results.append({
                    "国家": cty, "SKU": sku, "FNSKU": fnsku, "订单需求": to_int(qty),
                    "扣除数量": to_int(p_filled), "剩余缺口": to_int(qty - p_filled),
                    "扣除详情": status_str,
                    "加工说明": f"加工{to_int(p_proc['qty'])} (源:{','.join(set(p_proc['fnsku']))})" if p_proc['qty'] > 0 else "-",
                    "初始库存快照": f"深:{to_int(snap['深仓'])} 外:{to_int(snap['外协'])} 云:{to_int(snap['云仓'])}"
                })

    df_plan_res = pd.DataFrame(plan_results)

    # --- 2. 主任务拆解 (Phase 1-4) ---
    tiers = {1: [], 2: [], 3: [], 4: []}
    calc_logs = []
    
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
        if is_new: priority = 1 if not is_us else 2
        else: priority = 3 if not is_us else 4
            
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
    strat_us = [('stock', '外协'), ('stock', '云仓'), ('stock', '深仓')] 
    strat_non_us = [('stock', '深仓'), ('stock', '外协'), ('stock', '云仓')]
    
    # --- 3. 梯队计算 (沿用 V30.0 逻辑) ---
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

    for p in range(1, 5):
        current_tasks = tiers[p]
        if not current_tasks: continue
        is_us_tier = (p == 2 or p == 4)
        
        if not is_us_tier:
            # Non-US
            for t in current_tasks:
                rem, u1, pr1, l1 = inv_mgr.execute_deduction(t['sku'], t['fnsku'], t['qty'], strat_non_us, 'mixed')
                update_task(t, rem, u1, pr1, l1)
                rem, u2, pr2, l2 = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, [('po', '采购订单')], 'po_any')
                update_task(t, rem, u2, pr2, l2)
                results_map[t['row_idx']] = t
                if rem > 0: t['logs'].append(f"缺口 {to_int(rem)}")
        else:
            # US: 4 Rounds
            for t in current_tasks: # R1
                rem = t['qty'] - t['filled']
                if rem <= 0: continue
                rem, u, p_, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_us, 'strict_only')
                update_task(t, rem, u, p_, [f"[R1]:{x}" for x in l])
            for t in current_tasks: # R2
                rem = t['qty'] - t['filled']
                if rem <= 0: continue
                rem, u, p_, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, [('po', '采购订单')], 'strict_only')
                update_task(t, rem, u, p_, [f"[R2]:{x}" for x in l])
            for t in current_tasks: # R3
                rem = t['qty'] - t['filled']
                if rem <= 0: continue
                rem, u, p_, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_us, 'process_only')
                update_task(t, rem, u, p_, [f"[R3]:{x}" for x in l])
            for t in current_tasks: # R4
                rem = t['qty'] - t['filled']
                if rem <= 0: continue
                rem, u, p_, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, [('po', '采购订单')], 'process_only')
                update_task(t, rem, u, p_, [f"[R4]:{x}" for x in l])
            
            for t in current_tasks:
                if t['filled'] < t['qty']: t['logs'].append(f"缺口 {to_int(t['qty'] - t['filled'])}")
                results_map[t['row_idx']] = t

    # --- 4. 构建主输出 ---
    output_rows = []
    display_map = {'深仓':'深仓库存', '外协':'外协仓库存', '云仓':'云仓库存', '采购订单':'采购订单'}
    display_order = ['深仓', '外协', '云仓', '采购订单']
    
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
                if val > 0: status_parts.append(f"{display_map[k]}{to_int(val)}")
            
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
                "剩_云仓": to_int(snap['云仓']), "剩_PO": to_int(snap['采购订单'])
            })
        else:
             out_row.update({"库存状态": "-", "最终发货数量": 0, "缺货与否": "-"})
        output_rows.append(out_row)

    return pd.DataFrame(output_rows), calc_logs, inv_mgr.cleaning_logs, df_plan_res

# ==========================================
# 5. UI 渲染
# ==========================================
if 'df_demand' not in st.session_state:
    st.session_state.df_demand = pd.DataFrame(columns=["标签", "国家", "SKU", "FNSKU", "数量", "运营", "店铺", "备注"])

col_main, col_side = st.columns([75, 25])

with col_main:
    st.subheader("1. 需求填报")
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
    f_plan = st.file_uploader("提货计划 (选填, 含'订单需求')", type=['xlsx', 'xls', 'csv'])
    
    if st.button("🚀 开始计算", type="primary", use_container_width=True):
        if f_inv and f_po and not edited_df.empty:
            with st.spinner("执行双阶段清算..."):
                df_inv_raw, err1 = load_and_find_header(f_inv, "库存")
                df_po_raw, err2 = load_and_find_header(f_po, "PO")
                df_plan_raw, _ = load_and_find_header(f_plan, "计划")
                
                if err1: st.error(err1)
                elif err2: st.error(err2)
                else:
                    mgr = InventoryManager(df_inv_raw, df_po_raw)
                    final_df, logs, cleans, plan_res = run_allocation(edited_df, mgr, df_plan_raw, mapping)
                    
                    st.success("计算完成!")
                    
                    tab1, tab2, tab3, tab4 = st.tabs(["📋 主分配结果", "🚚 提货计划清算结果", "🔍 运算日志", "🧹 清洗日志"])
                    
                    with tab1:
                        def highlight(row):
                            if "缺货" in str(row.get('缺货与否', '')): return ['background-color: #ffcdd2'] * len(row)
                            return [''] * len(row)
                        st.dataframe(final_df.style.apply(highlight, axis=1), use_container_width=True)
                    
                    with tab2:
                        if not plan_res.empty:
                            st.dataframe(plan_res, use_container_width=True)
                        else:
                            st.info("无提货计划数据")
                            
                    with tab3: st.dataframe(pd.DataFrame(logs), use_container_width=True)
                    with tab4: st.dataframe(pd.DataFrame(cleans), use_container_width=True)
                    
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                        final_df.to_excel(writer, sheet_name='分配结果', index=False)
                        if not plan_res.empty:
                            plan_res.to_excel(writer, sheet_name='提货计划结果', index=False)
                        pd.DataFrame(logs).to_excel(writer, sheet_name='运算日志', index=False)
                        pd.DataFrame(cleans).to_excel(writer, sheet_name='清洗日志', index=False)
                    
                    st.download_button("📥 下载完整结果.xlsx", buf.getvalue(), "V31_Result_Full.xlsx")
        else:
            st.warning("请填写需求数据并上传库存文件")
            
