import streamlit as st
import pandas as pd
import io

# ==========================================
# 1. 基础配置
# ==========================================
st.set_page_config(page_title="智能调拨系统 V33.7 (底层去重版)", layout="wide", page_icon="🦁")

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
st.title("🦁 智能库存分配 V33.7 (底层真实去重 + FNSKU兜底)")

# ==========================================
# 2. 数据清洗与辅助函数
# ==========================================
def clean_number(x):
    if isinstance(x, pd.Series): x = x.iloc[0]
    if pd.isna(x): return 0
    s = str(x).strip().replace(',', '').replace(' ', '')
    try: return float(s)
    except: return 0

def to_int(x):
    try: return int(round(float(x)))
    except: return 0

def normalize_str(s):
    if isinstance(s, pd.Series): s = s.iloc[0]
    if pd.isna(s): return ""
    return str(s).strip().upper()

def normalize_wh_name(name):
    n = normalize_str(name)
    if "深" in n: return "深仓"
    if "外协" in n: return "外协"
    if "云" in n or "天源" in n: return "云仓"
    return "其他" 

def load_and_find_header(file):
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
            
        orig_cols = [str(c).upper().replace(' ', '') for c in df.columns]
        has_sku = any("SKU" in c or "编码" in c for c in orig_cols)
        
        if not has_sku:
            header_idx = -1
            for i, row in df.head(30).iterrows():
                row_vals = [str(v).upper().replace(' ', '') for v in row.values]
                if any("SKU" in v or "编码" in v for v in row_vals):
                    header_idx = i
                    break
            if header_idx != -1:
                df.columns = df.iloc[header_idx]
                df = df.iloc[header_idx+1:]
        
        df.reset_index(drop=True, inplace=True)
        
        raw_cols = [str(c).strip() for c in df.columns]
        seen = {}
        new_cols = []
        for c in raw_cols:
            if c in seen:
                seen[c] += 1
                new_cols.append(f"{c}_{seen[c]}") 
            else:
                seen[c] = 0
                new_cols.append(c)
        df.columns = new_cols
        
        df.dropna(how='all', inplace=True)
        return df, None
    except Exception as e:
        return None, f"读取错误: {str(e)}"

# ==========================================
# 3. 核心：库存管理器 (包含去重洗牌)
# ==========================================
class InventoryManager:
    def __init__(self, df_inv, df_po, df_plan):
        self.stock = {} 
        self.inbound = {} 
        self.cleaning_logs = []
        
        self._init_inventory(df_inv)
        self._init_inbound(df_po, '采购订单')
        self._init_inbound(df_plan, '提货计划')
        
        # --- 新增：物理层面的真实去重 ---
        self._deduct_plan_from_po()

    def _match_col(self, df, keywords):
        for k in keywords:
            for col in df.columns:
                col_clean = str(col).upper().replace(' ', '').replace('\n', '').replace('\r', '')
                if k in col_clean:
                    return col
        return None

    def _init_inventory(self, df):
        if df is None or df.empty: return
        
        c_sku = self._match_col(df, ['SKU', '编码', '代码', '型号'])
        c_fnsku = self._match_col(df, ['FNSKU', '条码', '标签', '贴标要求'])
        c_wh = self._match_col(df, ['仓库'])
        c_zone = self._match_col(df, ['库位', '库区', 'ZONE'])
        c_qty = self._match_col(df, ['可用', '数量', '库存'])

        if not (c_sku and c_wh and c_qty): return

        for idx, row in df.iterrows():
            w_name_raw = str(row.get(c_wh, ''))
            w_name_norm = normalize_str(w_name_raw)
            sku = str(row.get(c_sku, '')).strip().upper() 
            
            if any(k in w_name_norm for k in ["沃尔玛", "WALMART", "TEMU"]): continue
            if not sku: continue
            
            f_raw = row.get(c_fnsku, '')
            fnsku = str(f_raw).strip().upper() if pd.notna(f_raw) else ""
            qty = clean_number(row.get(c_qty, 0))
            zone = str(row.get(c_zone, '')).strip() if c_zone else "-"
            
            if qty <= 0: continue
            
            w_type = normalize_wh_name(w_name_raw)
            if sku not in self.stock: self.stock[sku] = {}
            if fnsku not in self.stock[sku]: self.stock[sku][fnsku] = {'深仓':[], '外协':[], '云仓':[], '采购订单':[], '其他':[]}
            self.stock[sku][fnsku][w_type].append({'qty': qty, 'raw_name': w_name_raw, 'zone': zone})

    def _init_inbound(self, df, source_type):
        if df is None or df.empty: return
        
        c_sku = self._match_col(df, ['SKU', '编码', '代码', '型号'])
        c_fnsku = self._match_col(df, ['FNSKU', '贴标要求', '条码', '标签'])
        c_qty = self._match_col(df, ['未入库', '未交', '在途', '数量', 'QTY', '需求'])
        c_req = self._match_col(df, ['需求人', '业务员', '人', '员'])
        
        if not c_sku or not c_qty:
            self.cleaning_logs.append({"类型": "致命错误(漏数据)", "SKU": "-", "原因": f"【{source_type}表】未识别到核心列！"})
            return
            
        block_list = ["陈丹丹", "张萍", "杨上儒", "陈炜填", "贝少婷", "詹翠萍"]
        
        for idx, row in df.iterrows():
            sku = str(row.get(c_sku, '')).strip().upper() 
            if source_type == '采购订单' and c_req:
                req = str(row.get(c_req, ''))
                if any(b in req for b in block_list): continue
            
            qty = clean_number(row.get(c_qty, 0))
            f_raw = row.get(c_fnsku, '')
            fnsku = str(f_raw).strip().upper() if pd.notna(f_raw) else ""
            
            if sku and qty > 0:
                if sku not in self.inbound: self.inbound[sku] = {}
                if fnsku not in self.inbound[sku]: self.inbound[sku][fnsku] = []
                self.inbound[sku][fnsku].append({'qty': qty, 'raw_name': source_type, 'zone': '-'})

    # --- 核心新增：物理扣减 PO ---
    def _deduct_plan_from_po(self):
        """核心去重逻辑：提货计划是PO的子集，必须从物理库存池中抹去，避免双重计算"""
        for sku in self.inbound:
            # 找到该SKU下所有的提货计划
            for plan_fnsku in list(self.inbound[sku].keys()):
                for plan_item in self.inbound[sku][plan_fnsku]:
                    if plan_item['raw_name'] != '提货计划': continue
                    
                    qty_to_deduct = plan_item['qty']
                    if qty_to_deduct <= 0: continue
                    
                    # 1. 精准维度：优先找 FNSKU 一模一样的采购订单扣减
                    if plan_fnsku in self.inbound[sku]:
                        for po_item in self.inbound[sku][plan_fnsku]:
                            if po_item['raw_name'] != '采购订单': continue
                            if qty_to_deduct <= 0: break
                            take = min(po_item['qty'], qty_to_deduct)
                            po_item['qty'] -= take
                            qty_to_deduct -= take
                            
                    # 2. 兜底维度：如果还有没扣完的(比如PO里填的是"新品")，强行找同SKU下的任何PO扣减
                    if qty_to_deduct > 0:
                        for other_fnsku in self.inbound[sku]:
                            if qty_to_deduct <= 0: break
                            for po_item in self.inbound[sku][other_fnsku]:
                                if po_item['raw_name'] != '采购订单': continue
                                if qty_to_deduct <= 0: break
                                take = min(po_item['qty'], qty_to_deduct)
                                po_item['qty'] -= take
                                qty_to_deduct -= take

    def get_snapshot(self, sku):
        res = {'深仓':0, '外协':0, '云仓':0, '采购订单': 0, '提货计划': 0}
        if sku in self.stock:
            for f in self.stock[sku]:
                for w_type in ['深仓', '外协', '云仓']:
                    res[w_type] += sum(item['qty'] for item in self.stock[sku][f].get(w_type, []))
        if sku in self.inbound:
            for f in self.inbound[sku]:
                for item in self.inbound[sku][f]:
                    if item['raw_name'] == '采购订单': res['采购订单'] += item['qty']
                    elif item['raw_name'] == '提货计划': res['提货计划'] += item['qty']
        return res

    def execute_deduction(self, sku, target_fnsku, qty_needed, strategy_chain, mode='strict_only'):
        qty_remain = qty_needed
        process_details = {'raw_wh': [], 'zone': [], 'fnsku': [], 'qty': 0}
        deduction_log = []
        usage_breakdown = {}
        
        for src_type, src_name in strategy_chain:
            if qty_remain <= 0: break
            step_taken = 0
            
            # --- 1. STOCK 扣减 ---
            if src_type == 'stock' and sku in self.stock:
                if mode in ['mixed', 'strict_only']:
                    if target_fnsku in self.stock[sku]:
                        for item in self.stock[sku][target_fnsku].get(src_name, []):
                            if qty_remain <= 0: break
                            if item['qty'] <= 0: continue
                            take = min(item['qty'], qty_remain)
                            item['qty'] -= take
                            qty_remain -= take
                            step_taken += take
                            deduction_log.append(f"{src_name}(直发,-{to_int(take)})")
                
                if mode in ['mixed', 'process_only'] and (qty_remain > 0 or mode == 'process_only'):
                    if qty_remain > 0:
                        for other_f in self.stock[sku]:
                            if other_f == target_fnsku: continue
                            if qty_remain <= 0: break
                            for item in self.stock[sku][other_f].get(src_name, []):
                                if qty_remain <= 0: break
                                if item['qty'] <= 0: continue
                                take = min(item['qty'], qty_remain)
                                item['qty'] -= take
                                qty_remain -= take
                                step_taken += take
                                process_details['raw_wh'].append(item['raw_name'])
                                process_details['zone'].append(item['zone'])
                                process_details['fnsku'].append(other_f)
                                process_details['qty'] += take
                                deduction_log.append(f"{src_name}(加工,-{to_int(take)})")

            # --- 2. 供应 (PO/Plan) 扣减 ---
            elif src_type == 'inbound' and sku in self.inbound:
                if mode in ['inbound_any', 'strict_only']:
                    targets = [target_fnsku] if mode == 'strict_only' else list(self.inbound[sku].keys())
                    for f in targets:
                        if f not in self.inbound[sku]: continue
                        if qty_remain <= 0: break
                        for item in self.inbound[sku][f]:
                            if item['raw_name'] != src_name: continue
                            if qty_remain <= 0: break
                            if item['qty'] <= 0: continue
                            take = min(item['qty'], qty_remain)
                            item['qty'] -= take
                            qty_remain -= take
                            step_taken += take
                            tag = f"{src_name}精准" if mode == 'strict_only' else f"{src_name}盲配"
                            deduction_log.append(f"{tag}(-{to_int(take)})")

                elif mode == 'process_only' and qty_remain > 0:
                    for other_f in self.inbound[sku]:
                        if other_f == target_fnsku: continue
                        if qty_remain <= 0: break
                        for item in self.inbound[sku][other_f]:
                            if item['raw_name'] != src_name: continue
                            if qty_remain <= 0: break
                            if item['qty'] <= 0: continue
                            take = min(item['qty'], qty_remain)
                            item['qty'] -= take
                            qty_remain -= take
                            step_taken += take
                            process_details['raw_wh'].append(src_name)
                            process_details['zone'].append('-')
                            process_details['fnsku'].append(other_f)
                            process_details['qty'] += take
                            deduction_log.append(f"{src_name}加工(-{to_int(take)})")
            
            if step_taken > 0:
                usage_breakdown[src_name] = usage_breakdown.get(src_name, 0) + step_taken

        return qty_remain, usage_breakdown, process_details, deduction_log

# ==========================================
# 4. 主逻辑流程
# ==========================================
def run_allocation(df_input, inv_mgr, mapping):
    
    col_sku = mapping['SKU']
    col_qty = mapping['数量']
    col_tag = mapping['标签']
    col_country = mapping['国家']
    col_fnsku = mapping['FNSKU']
    
    for idx in df_input.index:
        df_input.at[idx, col_sku] = str(df_input.at[idx, col_sku]).strip().upper()
        df_input.at[idx, col_fnsku] = str(df_input.at[idx, col_fnsku]).strip().upper()

    # === Step 0. 全局供需预判 ===
    df_input['__clean_qty'] = df_input[col_qty].apply(clean_number)
    demand_summary = df_input.groupby(col_sku)['__clean_qty'].sum().to_dict()
    df_input.drop(columns=['__clean_qty'], inplace=True)
    
    order_list = []
    for sku, req_qty in demand_summary.items():
        if req_qty <= 0 or not sku: continue
        
        # 此时底层拿到的PO，已经是洗掉提货计划后的“净PO”了
        total_stock = 0
        if sku in inv_mgr.stock:
            for f in inv_mgr.stock[sku]:
                total_stock += sum(i['qty'] for w in inv_mgr.stock[sku][f] for i in inv_mgr.stock[sku][f][w])
        
        total_po = 0
        total_plan = 0
        if sku in inv_mgr.inbound:
            for f in inv_mgr.inbound[sku]:
                for i in inv_mgr.inbound[sku][f]:
                    if i['raw_name'] == '采购订单': total_po += i['qty']
                    elif i['raw_name'] == '提货计划': total_plan += i['qty']
        
        # 因此，供应总和直接相加即可，不需要再减去重叠
        total_supply = total_stock + total_po + total_plan
        gap = req_qty - total_supply
        
        if gap > 0:
            order_list.append({
                "SKU": sku, 
                "总需求": to_int(req_qty),
                "国内库存": to_int(total_stock),
                "净PO未入库(已去重)": to_int(total_po),
                "提货计划量": to_int(total_plan),
                "总有效供应": to_int(total_supply),
                "建议真实下单数量": to_int(gap)
            })
    df_order_advice = pd.DataFrame(order_list)

    # === Step 1. 任务拆解与梯队隔离 ===
    tiers = {1: [], 2: []} 
    calc_logs = []
    
    for idx, row in df_input.iterrows():
        tag = str(row.get(col_tag, '')).strip()
        country = str(row.get(col_country, '')).strip()
        sku = str(row.get(col_sku, '')).strip()
        fnsku = str(row.get(col_fnsku, '')).strip()
        qty = clean_number(row.get(col_qty, 0))
        
        if qty <= 0 or not sku: continue
        
        is_us = 'US' in country.upper() or '美国' in country
        priority = 2 if is_us else 1
            
        task = {
            'row_idx': idx, 'priority': priority, 'sku': sku, 'fnsku': fnsku, 'qty': qty, 
            'country': country, 'is_us': is_us, 'tag': tag,
            'filled': 0, 'usage': {}, 'proc': {'raw_wh': [], 'zone': [], 'fnsku': [], 'qty': 0}, 'logs': []
        }
        tiers[priority].append(task)

    for p in [1, 2]:
        tiers[p].sort(key=lambda x: 0 if '新增' in x['tag'] else 1)

    results_map = {}
    
    strat_stock_us = [('stock', '外协'), ('stock', '云仓'), ('stock', '深仓')]
    strat_stock_non_us = [('stock', '深仓'), ('stock', '外协'), ('stock', '云仓')]
    strat_inbound = [('inbound', '提货计划'), ('inbound', '采购订单')]

    def update_task(t, rem, usage, proc, logs):
        step_fill = (t['qty'] - t['filled']) - rem
        t['filled'] += step_fill
        for k, v in usage.items(): t['usage'][k] = t['usage'].get(k, 0) + v
        if logs: t['logs'].extend(logs)
        if proc:
            t['proc']['raw_wh'].extend(proc['raw_wh']); t['proc']['zone'].extend(proc['zone'])
            t['proc']['fnsku'].extend(proc['fnsku']); t['proc']['qty'] += proc['qty']

    # === Step 2. 梯队全局扫描分配 ===
    
    # --- Tier 1: 非 US ---
    if tiers[1]:
        for t in tiers[1]: # R1: 现货精准
            rem = t['qty'] - t['filled']
            if rem > 0:
                r, u, p, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_stock_non_us, 'strict_only')
                update_task(t, r, u, p, [f"[R1现货精准]:{x}" for x in l])
        for t in tiers[1]: # R2: 现货加工
            rem = t['qty'] - t['filled']
            if rem > 0:
                r, u, p, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_stock_non_us, 'process_only')
                update_task(t, r, u, p, [f"[R2现货加工]:{x}" for x in l])
        for t in tiers[1]: # R3: 供应盲配
            rem = t['qty'] - t['filled']
            if rem > 0:
                r, u, p, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_inbound, 'inbound_any')
                update_task(t, r, u, p, [f"[R3供应盲配]:{x}" for x in l])
                
        for t in tiers[1]:
            if t['filled'] < t['qty']: t['logs'].append(f"缺口 {to_int(t['qty'] - t['filled'])}")
            results_map[t['row_idx']] = t
            calc_logs.append({"优先级": "Tier 1(非US)", "SKU": t['sku'], "FNSKU": t['fnsku'], "国家": t['country'], "执行过程": " | ".join(t['logs']), "发货": to_int(t['filled'])})

    # --- Tier 2: US ---
    if tiers[2]:
        for t in tiers[2]: # R1: 现货精准
            rem = t['qty'] - t['filled']
            if rem > 0:
                r, u, p, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_stock_us, 'strict_only')
                update_task(t, r, u, p, [f"[R1现货精准]:{x}" for x in l])
        for t in tiers[2]: # R2: 供应精准
            rem = t['qty'] - t['filled']
            if rem > 0:
                r, u, p, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_inbound, 'strict_only')
                update_task(t, r, u, p, [f"[R2供应精准]:{x}" for x in l])
        for t in tiers[2]: # R3: 现货加工
            rem = t['qty'] - t['filled']
            if rem > 0:
                r, u, p, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_stock_us, 'process_only')
                update_task(t, r, u, p, [f"[R3现货加工]:{x}" for x in l])
        for t in tiers[2]: # R4: 供应加工
            rem = t['qty'] - t['filled']
            if rem > 0:
                r, u, p, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_inbound, 'process_only')
                update_task(t, r, u, p, [f"[R4供应加工]:{x}" for x in l])

        for t in tiers[2]:
            if t['filled'] < t['qty']: t['logs'].append(f"缺口 {to_int(t['qty'] - t['filled'])}")
            results_map[t['row_idx']] = t
            calc_logs.append({"优先级": "Tier 2(US)", "SKU": t['sku'], "FNSKU": t['fnsku'], "国家": t['country'], "执行过程": " | ".join(t['logs']), "发货": to_int(t['filled'])})

    # === Step 3. 构建输出 ===
    output_rows = []
    display_order = ['深仓', '外协', '云仓', '提货计划', '采购订单']
    display_map = {'深仓':'深仓库存', '外协':'外协仓库存', '云仓':'云仓库存', '提货计划':'提货计划', '采购订单':'采购订单'}
    
    sku_shortage_map = {} 
    for idx, row in df_input.iterrows():
        t = results_map.get(idx)
        if t and (t['qty'] - t['filled'] > 0.001): 
            sku_shortage_map[t['sku']] = sku_shortage_map.get(t['sku'], 0) + (t['qty'] - t['filled'])
            
    for idx, row in df_input.iterrows():
        t = results_map.get(idx)
        out_row = row.to_dict()
        if t:
            status_parts = []
            transfer_note = ""
            for k in display_order:
                val = t['usage'].get(k, 0)
                if val > 0: 
                    s_text = f"{display_map[k]}{to_int(val)}"
                    if not t['is_us'] and k == '外协': transfer_note = "需调回深仓"
                    status_parts.append(s_text)
            
            status_str = "+".join(status_parts)
            if t['filled'] < t['qty']: 
                status_str += f"+待下单(缺{to_int(t['qty'] - t['filled'])})" if status_str else "待下单"
            
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
                "采购订单数量": to_int(t['usage'].get('采购订单', 0)), 
                "调拨提示": transfer_note,
                "缺货与否": short_stat,
                "加工库区": p_wh, "加工库区_库位": p_zone, "加工FNSKU": p_fn, "加工数量": p_qt,
                "剩_深仓": to_int(snap['深仓']), "剩_外协": to_int(snap['外协']),
                "剩_云仓": to_int(snap['云仓']), "剩_计划": to_int(snap['提货计划']), "剩_净PO": to_int(snap['采购订单'])
            })
        else:
             out_row.update({"库存状态": "-", "最终发货数量": 0, "采购订单数量": 0, "调拨提示": "", "缺货与否": "-"})
        output_rows.append(out_row)

    return pd.DataFrame(output_rows), calc_logs, inv_mgr.cleaning_logs, df_order_advice

# ==========================================
# 5. UI 渲染
# ==========================================
if 'df_demand' not in st.session_state:
    st.session_state.df_demand = pd.DataFrame(columns=["标签", "国家", "SKU", "FNSKU", "数量", "运营", "店铺", "备注"])

col_main, col_side = st.columns([75, 25])

with col_main:
    st.subheader("1. 需求填报 (V33.7 底层去重版)")
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
    map_qty = c5.selectbox("数量列", cols, index=get_idx(['数量', '需求']))
    mapping = {'标签': map_tag, '国家': map_country, 'SKU': map_sku, 'FNSKU': map_fnsku, '数量': map_qty}

with col_side:
    st.subheader("2. 资源文件上传")
    f_inv = st.file_uploader("A. 库存表 (在库)", type=['xlsx', 'xls', 'csv'])
    f_po = st.file_uploader("B. 采购追踪表 (在途/PO)", type=['xlsx', 'xls', 'csv'])
    f_plan = st.file_uploader("C. 提货计划表 (选填)", type=['xlsx', 'xls', 'csv'])
    
    if st.button("🚀 执行全局智能分配", type="primary", use_container_width=True):
        if f_inv and f_po and not edited_df.empty:
            with st.spinner("执行底层物理去重及分配引擎..."):
                df_inv_raw, err1 = load_and_find_header(f_inv)
                df_po_raw, err2 = load_and_find_header(f_po)
                df_plan_raw, _ = load_and_find_header(f_plan)
                
                if err1: st.error(err1)
                elif err2: st.error(err2)
                else:
                    mgr = InventoryManager(df_inv_raw, df_po_raw, df_plan_raw)
                    final_df, logs, cleans, order_advice = run_allocation(edited_df, mgr, mapping)
                    
                    st.success("运算完成！👉 PO已完成物理去重，请查看结果。")
                    
                    if not order_advice.empty:
                        st.error(f"⚠️ 预警：发现 {len(order_advice)} 个需要真实补单的 SKU（已剔除提货计划）！")
                        st.dataframe(order_advice, use_container_width=True)
                    else:
                        st.success("✅ 供需平衡，全盘供应可满足所有需求。")
                    
                    tab1, tab2, tab3 = st.tabs(["📋 分配结果明细", "🔍 运算逻辑日志", "✅ 数据诊断雷达"])
                    
                    with tab1:
                        def highlight(row):
                            if "缺货" in str(row.get('缺货与否', '')): return ['background-color: #ffcdd2'] * len(row)
                            return [''] * len(row)
                        st.dataframe(final_df.style.apply(highlight, axis=1), use_container_width=True)
                    
                    with tab2: st.dataframe(pd.DataFrame(logs), use_container_width=True)
                    with tab3: st.dataframe(pd.DataFrame(cleans), use_container_width=True)
                    
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                        final_df.to_excel(writer, sheet_name='分配结果', index=False)
                        if not order_advice.empty: order_advice.to_excel(writer, sheet_name='待下单清单', index=False)
                        pd.DataFrame(logs).to_excel(writer, sheet_name='运算日志', index=False)
                        pd.DataFrame(cleans).to_excel(writer, sheet_name='清洗诊断日志', index=False)
                    
                    st.download_button("📥 下载完整报告.xlsx", buf.getvalue(), "V33_7_Result.xlsx")
        else:
            st.warning("请在左侧填写需求数据，并在右侧上传库存和PO文件。")
