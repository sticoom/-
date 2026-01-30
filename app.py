import streamlit as st
import pandas as pd
import io
import copy

# ==========================================
# 1. 基础配置
# ==========================================
st.set_page_config(page_title="智能调拨系统 V31.0 (计划独立预扣)", layout="wide", page_icon="🦁")

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
st.title("🦁 智能库存分配 V31.0 (提货计划精准预扣 + 全局统筹)")

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
        # 扩大搜索范围以适应复杂表头
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
# 3. 核心：库存管理器
# ==========================================
class InventoryManager:
    def __init__(self, df_inv, df_po):
        self.stock = {} 
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
        process_details = {'raw_wh': [], 'zone': [], 'fnsku': [], 'qty': 0}
        deduction_log = []
        usage_breakdown = {}
        
        for src_type, src_name in strategy_chain:
            if qty_remain <= 0: break
            step_taken = 0
            
            # --- STOCK 处理 ---
            if src_type == 'stock' and sku in self.stock:
                # A. 混合 / 严格模式 (同 FNSKU)
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
                
                # B. 混合 / 加工模式 (异 FNSKU)
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
                # A. 任意 / 严格
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

                # B. 加工
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

        return qty_remain, usage_breakdown, process_details, deduction_log

# ==========================================
# 4. 主逻辑流程
# ==========================================
def run_allocation(df_input, inv_mgr, df_plan, mapping):
    tiers = {1: [], 2: [], 3: [], 4: []}
    calc_logs = []
    
    # 策略定义
    strat_us = [('stock', '外协'), ('stock', '云仓'), ('stock', '深仓')] 
    strat_non_us = [('stock', '深仓'), ('stock', '外协'), ('stock', '云仓')]
    
    # ===============================
    # Phase 0: 提货计划 (Tier -1)
    # ===============================
    plan_output = []
    
    if df_plan is not None and not df_plan.empty:
        # 列名适配 (兼容 Order Demand / 订单需求)
        c_sku = next((c for c in df_plan.columns if 'SKU' in c.upper()), None)
        c_fnsku = next((c for c in df_plan.columns if 'FNSKU' in c.upper()), None)
        c_country = next((c for c in df_plan.columns if '国家' in c), None)
        # 优先找"订单需求"，没有再找"计划"或"数量"
        c_qty = next((c for c in df_plan.columns if '订单需求' in c), None)
        if not c_qty: c_qty = next((c for c in df_plan.columns if '计划' in c), None)
        if not c_qty: c_qty = next((c for c in df_plan.columns if '数量' in c), None)
        
        if c_sku and c_qty:
            for idx, row in df_plan.iterrows():
                sku = str(row.get(c_sku, '')).strip()
                f_raw = row.get(c_fnsku, '') if c_fnsku else ''
                fnsku = str(f_raw).strip() if pd.notna(f_raw) else ""
                qty = clean_number(row.get(c_qty, 0))
                country = str(row.get(c_country, '-'))
                
                if qty <= 0: continue
                
                # 计划策略：四轮全局 (确保精准匹配 FNSKU)
                # 无论国家，都为了满足 FNSKU
                # R1 Strict Stock -> R2 Strict PO -> R3 Process Stock -> R4 Process PO
                
                fill_total = 0
                usage_total = {}
                logs_total = []
                
                # 1. R1 Strict Stock
                rem, usage, _, logs = inv_mgr.execute_deduction(sku, fnsku, qty, strat_stock := [('stock','深仓'),('stock','外协'),('stock','云仓')], 'strict_only')
                fill_total += (qty - rem)
                for k,v in usage.items(): usage_total[k] = usage_total.get(k,0)+v
                logs_total.extend(logs)
                
                # 2. R2 Strict PO
                if rem > 0:
                    old_rem = rem
                    rem, usage, _, logs = inv_mgr.execute_deduction(sku, fnsku, rem, [('po','采购订单')], 'strict_only')
                    fill_total += (old_rem - rem)
                    for k,v in usage.items(): usage_total[k] = usage_total.get(k,0)+v
                    logs_total.extend(logs)
                    
                # 3. R3 Process Stock
                if rem > 0:
                    old_rem = rem
                    rem, usage, _, logs = inv_mgr.execute_deduction(sku, fnsku, rem, strat_stock, 'process_only')
                    fill_total += (old_rem - rem)
                    for k,v in usage.items(): usage_total[k] = usage_total.get(k,0)+v
                    logs_total.extend(logs)
                    
                # 4. R4 Process PO
                if rem > 0:
                    old_rem = rem
                    rem, usage, _, logs = inv_mgr.execute_deduction(sku, fnsku, rem, [('po','采购订单')], 'process_only')
                    fill_total += (old_rem - rem)
                    for k,v in usage.items(): usage_total[k] = usage_total.get(k,0)+v
                    logs_total.extend(logs)
                
                # 记录结果
                status_parts = []
                display_map = {'深仓':'深仓', '外协':'外协', '云仓':'云仓', '采购订单':'PO'}
                for k in ['深仓','外协','云仓','采购订单']:
                    val = usage_total.get(k, 0)
                    if val > 0: status_parts.append(f"{display_map[k]}{to_int(val)}")
                
                plan_output.append({
                    "国家": country, "SKU": sku, "FNSKU": fnsku, "计划需求": to_int(qty),
                    "实际扣除": to_int(fill_total),
                    "扣除明细": "+".join(status_parts),
                    "缺口": to_int(rem) if rem > 0 else "-"
                })

    # --- 2. 需求任务初始化 ---
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
    
    # --- 3. 需求梯队计算 (Phase 1-4) ---
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
            # === Non-US: 瀑布流 + PO任意 ===
            for t in current_tasks:
                # R1+R2: Stock Mixed
                rem, usage1, proc1, logs1 = inv_mgr.execute_deduction(t['sku'], t['fnsku'], t['qty'], strat_non_us, 'mixed')
                update_task(t, rem, usage1, proc1, logs1)
                
                # R3: PO Any
                rem, usage2, proc2, logs2 = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, [('po', '采购订单')], 'po_any')
                update_task(t, rem, usage2, proc2, logs2)
                
                results_map[t['row_idx']] = t
                calc_logs.append({
                    "步骤": f"Tier {p} (Non-US)", "SKU": t['sku'], "需求": to_int(t['qty']),
                    "执行过程": " || ".join(t['logs']), "最终发货": to_int(t['filled'])
                })

        else:
            # === US: 全局四轮 ===
            # R1: Strict Stock
            for t in current_tasks:
                rem = t['qty'] - t['filled']
                if rem <= 0: continue
                rem, usage, proc, logs = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_us, 'strict_only')
                update_task(t, rem, usage, proc, [f"[R1]:{l}" for l in logs])
                
            # R2: Strict PO
            for t in current_tasks:
                rem = t['qty'] - t['filled']
                if rem <= 0: continue
                rem, usage, proc, logs = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, [('po', '采购订单')], 'strict_only')
                update_task(t, rem, usage, proc, [f"[R2]:{l}" for l in logs])
                
            # R3: Process Stock
            for t in current_tasks:
                rem = t['qty'] - t['filled']
                if rem <= 0: continue
                rem, usage, proc, logs = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_us, 'process_only')
                update_task(t, rem, usage, proc, [f"[R3]:{l}" for l in logs])
                
            # R4: Process PO
            for t in current_tasks:
                rem = t['qty'] - t['filled']
                if rem <= 0: continue
                rem, usage, proc, logs = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, [('po', '采购订单')], 'process_only')
                update_task(t, rem, usage, proc, [f"[R4]:{l}" for l in logs])
            
            for t in current_tasks:
                results_map[t['row_idx']] = t
                calc_logs.append({
                    "步骤": f"Tier {p} (US全局)", "SKU": t['sku'], "需求": to_int(t['qty']),
                    "执行过程": " || ".join(t['logs']), "最终发货": to_int(t['filled'])
                })

    # --- 4. 构建输出 ---
    output_rows = []
    
    sku_shortage_map = {} 
    for idx, row in df_input.iterrows():
        t = results_map.get(idx)
        if t:
            gap = t['qty'] - t['filled']
            if gap > 0.001:
                sku_shortage_map[t['sku']] = sku_shortage_map.get(t['sku'], 0) + gap
            
    for idx, row in df_input.iterrows():
        t = results_map.get(idx)
        out_row = row.to_dict()
        
        if t:
            status_parts = []
            display_order = ['深仓', '外协', '云仓', '采购订单']
            display_map = {'深仓':'深仓库存', '外协':'外协仓库存', '云仓':'云仓库存', '采购订单':'采购订单'}
            
            for k in display_order:
                val = t['usage'].get(k, 0)
                if val > 0:
                    status_parts.append(f"{display_map[k]}{to_int(val)}")
            
            status_str = "+".join(status_parts)
            if t['filled'] < t['qty']:
                status_str += f"+待下单(缺{to_int(t['qty'] - t['filled'])})"
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

    df_out = pd.DataFrame(output_rows)
    df_calc_log = pd.DataFrame(calc_logs)
    df_clean_log = pd.DataFrame(inv_mgr.cleaning_logs)
    df_plan_res = pd.DataFrame(plan_output)
    
    if not df_out.empty and col_sku in df_out.columns:
        df_out.sort_values(by=[col_sku], inplace=True)
        base_cols = list(df_input.columns)
        calc_cols = ["库存状态", "最终发货数量", "缺货与否", 
                     "加工库区", "加工库区_库位", "加工FNSKU", "加工数量", 
                     "剩_深仓", "剩_外协", "剩_云仓", "剩_PO"]
        final_cols = base_cols + [c for c in calc_cols if c not in base_cols]
        df_out = df_out[final_cols]

    return df_out, df_calc_log, df_clean_log, df_plan_res

# ==========================================
# 5. UI 渲染
# ==========================================
if 'df_demand' not in st.session_state:
    st.session_state.df_demand = pd.DataFrame(columns=["标签", "国家", "SKU", "FNSKU", "数量", "运营", "店铺", "备注"])

col_main, col_side = st.columns([75, 25])

with col_main:
    st.subheader("1. 需求填报 (V31.0 计划独立版)")
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
    f_plan = st.file_uploader("计划表 (含'订单需求')", type=['xlsx', 'xls', 'csv'])
    
    if st.button("🚀 开始计算", type="primary", use_container_width=True):
        if f_inv and f_po and not edited_df.empty:
            with st.spinner("执行 Tier + Global + Plan Pre-deduction 算法..."):
                df_inv_raw, err1 = load_and_find_header(f_inv, "库存")
                df_po_raw, err2 = load_and_find_header(f_po, "PO")
                
                df_plan_raw = None
                if f_plan:
                    df_plan_raw, _ = load_and_find_header(f_plan, "计划")
                
                if err1: st.error(err1)
                elif err2: st.error(err2)
                else:
                    mgr = InventoryManager(df_inv_raw, df_po_raw)
                    final_df, df_calc, df_clean, df_plan_res = run_allocation(edited_df, mgr, df_plan_raw, mapping)
                    
                    st.success("计算完成!")
                    
                    # 结果展示 Tabs
                    tabs = ["📋 分配结果", "🔍 运算日志", "🧹 清洗日志"]
                    if not df_plan_res.empty: tabs.insert(0, "📅 提货计划结果")
                    
                    tab_objs = st.tabs(tabs)
                    
                    # 计划结果
                    if not df_plan_res.empty:
                        with tab_objs[0]:
                            st.write("### 提货计划扣减明细")
                            st.dataframe(df_plan_res, use_container_width=True)
                            
                    # 主结果
                    main_idx = 1 if not df_plan_res.empty else 0
                    with tab_objs[main_idx]:
                        def highlight(row):
                            if "缺货" in str(row.get('缺货与否', '')): return ['background-color: #ffcdd2'] * len(row)
                            return [''] * len(row)
                        st.dataframe(final_df.style.apply(highlight, axis=1), use_container_width=True)
                        
                    # 运算日志
                    with tab_objs[main_idx+1]:
                        st.dataframe(df_calc, use_container_width=True)
                    # 清洗日志
                    with tab_objs[main_idx+2]:
                        st.dataframe(df_clean, use_container_width=True)
                    
                    # Excel 下载
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                        final_df.to_excel(writer, sheet_name='分配结果', index=False)
                        if not df_plan_res.empty:
                            df_plan_res.to_excel(writer, sheet_name='提货计划结果', index=False)
                        df_calc.to_excel(writer, sheet_name='运算日志', index=False)
                        df_clean.to_excel(writer, sheet_name='清洗日志', index=False)
                        writer.sheets['分配结果'].freeze_panes(1, 0)
                    
                    st.download_button("📥 下载完整结果.xlsx", buf.getvalue(), "V31_Result_Full.xlsx")
        else:
            st.warning("请填写需求数据并上传库存文件")
