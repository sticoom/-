import streamlit as st
import pandas as pd
import io

# ==========================================
# 1. 基础配置
# ==========================================
st.set_page_config(page_title="智能调拨系统 V35.1 (白皮书定稿版)", layout="wide", page_icon="🦁")

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
st.title("🦁 智能库存分配 V35.1 (全局同步 + 底层洗盘)")

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
# 3. 核心：库存管理器 (前置车间数据净化)
# ==========================================
class InventoryManager:
    def __init__(self, df_inv, df_po, df_plan):
        self.stock = {} 
        self.po = {}
        self.plan = {}
        self.inbound = {} 
        self.cleaning_logs = []
        
        self._init_inventory(df_inv)
        self._init_po(df_po)
        self._init_plan(df_plan)
        
        # 底层去重：提货计划扣减PO
        self._deduct_plan_from_po()
        self._merge_inbound_for_allocation()

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
            
            # 黑名单过滤
            if any(k in w_name_norm for k in ["沃尔玛", "WALMART", "TEMU"]): 
                self.cleaning_logs.append({"类型": "库存过滤", "SKU": sku, "原因": f"剔除黑名单仓库 ({w_name_raw})"})
                continue
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

    def _init_po(self, df):
        if df is None or df.empty: return
        c_sku = self._match_col(df, ['SKU', '编码', '代码', '型号'])
        c_fnsku = self._match_col(df, ['FNSKU', '贴标要求', '条码', '标签'])
        c_qty = self._match_col(df, ['未入库', '未交', '在途', '数量', 'QTY', '需求'])
        c_req = self._match_col(df, ['需求人', '业务员', '人', '员'])
        
        if not c_sku or not c_qty: return
        block_list = ["陈丹丹", "张萍", "杨上儒", "陈炜填", "贝少婷", "詹翠萍"]
        
        for idx, row in df.iterrows():
            sku = str(row.get(c_sku, '')).strip().upper() 
            
            # 黑名单过滤
            if c_req:
                req = str(row.get(c_req, ''))
                if any(b in req for b in block_list):
                    self.cleaning_logs.append({"类型": "采购过滤", "SKU": sku, "原因": f"剔除黑名单需求人 ({req})"})
                    continue
                    
            qty = clean_number(row.get(c_qty, 0))
            f_raw = row.get(c_fnsku, '')
            fnsku = str(f_raw).strip().upper() if pd.notna(f_raw) else ""
            if sku and qty > 0:
                if sku not in self.po: self.po[sku] = {}
                if fnsku not in self.po[sku]: self.po[sku][fnsku] = []
                self.po[sku][fnsku].append({'qty': qty, 'raw_name': '采购订单', 'zone': '-'})

    def _init_plan(self, df):
        if df is None or df.empty: return
        c_sku = self._match_col(df, ['SKU', '编码', '代码', '型号'])
        c_fnsku = self._match_col(df, ['FNSKU', '贴标要求', '条码', '标签'])
        c_qty = self._match_col(df, ['数量', 'QTY', '需求'])
        
        if not c_sku or not c_qty: return
        for idx, row in df.iterrows():
            sku = str(row.get(c_sku, '')).strip().upper() 
            qty = clean_number(row.get(c_qty, 0))
            f_raw = row.get(c_fnsku, '')
            fnsku = str(f_raw).strip().upper() if pd.notna(f_raw) else ""
            if sku and qty > 0:
                if sku not in self.plan: self.plan[sku] = {}
                if fnsku not in self.plan[sku]: self.plan[sku][fnsku] = []
                self.plan[sku][fnsku].append({'qty': qty, 'raw_name': '提货计划', 'zone': '-'})

    def _deduct_plan_from_po(self):
        """物理层去重：用提货计划扣减原始PO，榨出净PO"""
        for sku, plan_fnsku_dict in self.plan.items():
            if sku not in self.po: continue 
                
            for plan_fnsku, plan_items in plan_fnsku_dict.items():
                for plan_item in plan_items:
                    qty_to_deduct = plan_item['qty']
                    if qty_to_deduct <= 0: continue
                    
                    # 1. 优先精准扣减
                    if plan_fnsku in self.po[sku]:
                        for po_item in self.po[sku][plan_fnsku]:
                            if qty_to_deduct <= 0: break
                            if po_item['qty'] <= 0: continue
                            take = min(po_item['qty'], qty_to_deduct)
                            po_item['qty'] -= take
                            qty_to_deduct -= take
                            if take > 0: self.cleaning_logs.append({"类型": "底层去重(精准)", "SKU": sku, "原因": f"同标(FNSKU:{plan_fnsku}) PO扣除了量: {take}"})
                            
                    # 2. 兜底宽泛扣减
                    if qty_to_deduct > 0:
                        for other_fnsku, po_items in self.po[sku].items():
                            if qty_to_deduct <= 0: break
                            for po_item in po_items:
                                if qty_to_deduct <= 0: break
                                if po_item['qty'] <= 0: continue
                                take = min(po_item['qty'], qty_to_deduct)
                                po_item['qty'] -= take
                                qty_to_deduct -= take
                                if take > 0: self.cleaning_logs.append({"类型": "底层去重(兜底)", "SKU": sku, "原因": f"跨标/通货(PO标:{other_fnsku}) 垫付扣除量: {take}"})

    def _merge_inbound_for_allocation(self):
        self.inbound = {}
        for sku in self.plan:
            if sku not in self.inbound: self.inbound[sku] = {}
            for fnsku in self.plan[sku]:
                if fnsku not in self.inbound[sku]: self.inbound[sku][fnsku] = []
                self.inbound[sku][fnsku].extend(self.plan[sku][fnsku])
                
        for sku in self.po:
            if sku not in self.inbound: self.inbound[sku] = {}
            for fnsku in self.po[sku]:
                if fnsku not in self.inbound[sku]: self.inbound[sku][fnsku] = []
                valid_pos = [p for p in self.po[sku][fnsku] if p['qty'] > 0]
                self.inbound[sku][fnsku].extend(valid_pos)

    def get_total_supply(self, sku):
        total = 0
        if sku in self.stock:
            total += sum(i['qty'] for f in self.stock[sku] for w in self.stock[sku][f] for i in self.stock[sku][f][w])
        if sku in self.inbound:
            total += sum(i['qty'] for f in self.inbound[sku] for i in self.inbound[sku][f])
        return total

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
            
            # --- STOCK 扣减 ---
            if src_type == 'stock' and sku in self.stock:
                if mode in ['mixed', 'strict_only']:
                    if target_fnsku in self.stock[sku]:
                        for item in self.stock[sku][target_fnsku].get(src_name, []):
                            if qty_remain <= 0: break
                            if item['qty'] <= 0: continue
                            take = min(item['qty'], qty_remain)
                            item['qty'] -= take; qty_remain -= take; step_taken += take
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
                                item['qty'] -= take; qty_remain -= take; step_taken += take
                                process_details['raw_wh'].append(item['raw_name'])
                                process_details['zone'].append(item['zone'])
                                process_details['fnsku'].append(other_f)
                                process_details['qty'] += take
                                deduction_log.append(f"{src_name}(加工,-{to_int(take)})")

            # --- INBOUND 扣减 ---
            elif src_type == 'inbound' and sku in self.inbound:
                if mode == 'strict_only':
                    if target_fnsku in self.inbound[sku]:
                        for item in self.inbound[sku][target_fnsku]:
                            if item['raw_name'] != src_name: continue
                            if qty_remain <= 0: break
                            if item['qty'] <= 0: continue
                            take = min(item['qty'], qty_remain)
                            item['qty'] -= take; qty_remain -= take; step_taken += take
                            deduction_log.append(f"{src_name}精准(-{to_int(take)})")

                elif mode == 'process_only' and qty_remain > 0:
                    for other_f in self.inbound[sku]:
                        if other_f == target_fnsku: continue
                        if qty_remain <= 0: break
                        for item in self.inbound[sku][other_f]:
                            if item['raw_name'] != src_name: continue
                            if qty_remain <= 0: break
                            if item['qty'] <= 0: continue
                            take = min(item['qty'], qty_remain)
                            item['qty'] -= take; qty_remain -= take; step_taken += take
                            process_details['raw_wh'].append(src_name)
                            process_details['zone'].append('-')
                            process_details['fnsku'].append(other_f)
                            process_details['qty'] += take
                            deduction_log.append(f"{src_name}加工(-{to_int(take)})")
            
            if step_taken > 0:
                usage_breakdown[src_name] = usage_breakdown.get(src_name, 0) + step_taken

        return qty_remain, usage_breakdown, process_details, deduction_log

# ==========================================
# 4. 主逻辑流程 (分配引擎)
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
        total_supply = inv_mgr.get_total_supply(sku)
        snap = inv_mgr.get_snapshot(sku)
        gap = req_qty - total_supply
        
        if gap > 0:
            order_list.append({
                "SKU": sku, 
                "总需求": to_int(req_qty),
                "国内库存": to_int(snap['深仓'] + snap['外协'] + snap['云仓']),
                "净PO未入库(已清洗)": to_int(snap['采购订单']),
                "提货计划总量": to_int(snap['提货计划']),
                "总有效供应": to_int(total_supply),
                "建议补单缺口": to_int(gap)
            })
    df_order_advice = pd.DataFrame(order_list)

    # === Step 1. 任务统筹池 (全盘同频起跑) ===
    tasks = []
    calc_logs = []
    
    for idx, row in df_input.iterrows():
        tag = str(row.get(col_tag, '')).strip()
        country = str(row.get(col_country, '')).strip()
        sku = str(row.get(col_sku, '')).strip()
        fnsku = str(row.get(col_fnsku, '')).strip()
        qty = clean_number(row.get(col_qty, 0))
        
        if qty <= 0 or not sku: continue
        is_us = 'US' in country.upper() or '美国' in country
            
        tasks.append({
            'row_idx': idx, 'sku': sku, 'fnsku': fnsku, 'qty': qty, 
            'country': country, 'is_us': is_us, 'tag': tag,
            'filled': 0, 'usage': {}, 'proc': {'raw_wh': [], 'zone': [], 'fnsku': [], 'qty': 0}, 'logs': []
        })

    tasks.sort(key=lambda x: 0 if '新增' in x['tag'] else 1)
    results_map = {}
    
    strat_stock_us = [('stock', '外协'), ('stock', '云仓'), ('stock', '深仓')]
    strat_stock_non_us = [('stock', '深仓'), ('stock', '外协'), ('stock', '云仓')]
    strat_plan = [('inbound', '提货计划')]
    strat_po = [('inbound', '采购订单')]

    def update_task(t, rem, usage, proc, logs):
        step_fill = (t['qty'] - t['filled']) - rem
        t['filled'] += step_fill
        for k, v in usage.items(): t['usage'][k] = t['usage'].get(k, 0) + v
        if logs: t['logs'].extend(logs)
        if proc:
            t['proc']['raw_wh'].extend(proc['raw_wh']); t['proc']['zone'].extend(proc['zone'])
            t['proc']['fnsku'].extend(proc['fnsku']); t['proc']['qty'] += proc['qty']

    # === Step 2 & 3: 核心分配引擎 (6轮精密扫描) ===
    
    # 【第一阶段：全局保精准】
    for t in tasks: # Round 1: 全局现货精准
        rem = t['qty'] - t['filled']
        if rem > 0:
            strat = strat_stock_us if t['is_us'] else strat_stock_non_us
            r, u, p, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat, 'strict_only')
            update_task(t, r, u, p, [f"[R1现货精准]:{x}" for x in l])
            
    for t in tasks: # Round 2: 全局提货精准
        rem = t['qty'] - t['filled']
        if rem > 0:
            r, u, p, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_plan, 'strict_only')
            update_task(t, r, u, p, [f"[R2提货精准]:{x}" for x in l])

    # 【第二阶段：非 US 独享异标借用】
    for t in tasks: # Round 3: 非US 现货跨标
        if not t['is_us']:
            rem = t['qty'] - t['filled']
            if rem > 0:
                r, u, p, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_stock_non_us, 'process_only')
                update_task(t, r, u, p, [f"[R3现货跨标]:{x}" for x in l])
                
    for t in tasks: # Round 4: 非US 提货跨标
        if not t['is_us']:
            rem = t['qty'] - t['filled']
            if rem > 0:
                r, u, p, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_plan, 'process_only')
                update_task(t, r, u, p, [f"[R4提货跨标]:{x}" for x in l])

    # 【第三阶段：全局 PO 兜底】
    for t in tasks: # Round 5: 全局 PO 精准
        rem = t['qty'] - t['filled']
        if rem > 0:
            r, u, p, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_po, 'strict_only')
            update_task(t, r, u, p, [f"[R5采购精准]:{x}" for x in l])
            
    for t in tasks: # Round 6: 全局 PO 兜底
        rem = t['qty'] - t['filled']
        if rem > 0:
            r, u, p, l = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat_po, 'process_only')
            update_task(t, r, u, p, [f"[R6采购兜底]:{x}" for x in l])

    for t in tasks:
        if t['filled'] < t['qty']: t['logs'].append(f"缺口 {to_int(t['qty'] - t['filled'])}")
        results_map[t['row_idx']] = t
        calc_logs.append({"属性": "US" if t['is_us'] else "非US", "SKU": t['sku'], "FNSKU": t['fnsku'], "国家": t['country'], "执行过程": " | ".join(t['logs']), "发货": to_int(t['filled'])})

    # === Step 4. 输出端与缺货联动 ===
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
                    if not t['is_us'] and k in ['外协', '云仓']: transfer_note = "需调回深仓"
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
    st.subheader("1. 需求填报 (V35.1 白皮书定稿版)")
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
            with st.spinner("执行独立数据净化及双向分配引擎..."):
                df_inv_raw, err1 = load_and_find_header(f_inv)
                df_po_raw, err2 = load_and_find_header(f_po)
                df_plan_raw, _ = load_and_find_header(f_plan)
                
                if err1: st.error(err1)
                elif err2: st.error(err2)
                else:
                    mgr = InventoryManager(df_inv_raw, df_po_raw, df_plan_raw)
                    final_df, logs, cleans, order_advice = run_allocation(edited_df, mgr, mapping)
                    
                    st.success("运算完成！👉 全局同步竞争标的，请查看日志核对！")
                    
                    if not order_advice.empty:
                        st.error(f"⚠️ 预警：发现 {len(order_advice)} 个需要真实补单的 SKU（已扣除提货计划的PO量）！")
                        st.dataframe(order_advice, use_container_width=True)
                    else:
                        st.success("✅ 供需平衡，全盘供应可满足所有需求。")
                    
                    tab1, tab2, tab3 = st.tabs(["📋 分配结果明细", "🔍 运算逻辑日志", "✅ 清洗诊断日志"])
                    
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
                        pd.DataFrame(cleans).to_excel(writer, sheet_name='清洗去重日志', index=False)
                    
                    st.download_button("📥 下载完整报告.xlsx", buf.getvalue(), "V35_1_Result.xlsx")
        else:
            st.warning("请在左侧填写需求数据，并在右侧上传库存和PO文件。")
