import streamlit as st
import pandas as pd
import io

# ==========================================
# 1. 基础配置
# ==========================================
st.set_page_config(page_title="智能调拨系统 V35.6 (稳固基座+溯源版)", layout="wide", page_icon="👑")

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
st.title("👑 智能库存分配 V35.6 (回归 V35.3 引擎 + 财务溯源)")

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
# 3. 核心：库存管理器 (前置净化与去重)
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
        for sku, plan_fnsku_dict in self.plan.items():
            if sku not in self.po: continue 
            for plan_fnsku, plan_items in plan_fnsku_dict.items():
                for plan_item in plan_items:
                    qty_to_deduct = plan_item['qty']
                    if qty_to_deduct <= 0: continue
                    
                    if plan_fnsku in self.po[sku]:
                        for po_item in self.po[sku][plan_fnsku]:
                            if qty_to_deduct <= 0: break
                            if po_item['qty'] <= 0: continue
                            take = min(po_item['qty'], qty_to_deduct)
                            po_item['qty'] -= take
                            qty_to_deduct -= take
                            if take > 0: self.cleaning_logs.append({"类型": "底层去重(精准)", "SKU": sku, "原因": f"同标(FNSKU:{plan_fnsku}) PO扣除了量: {take}"})
                            
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
        
    def get_exact_qty(self, src_type, src_name, sku, fnsku):
        if src_type == 'stock':
            return sum(i['qty'] for i in self.stock.get(sku, {}).get(fnsku, {}).get(src_name, []))
        elif src_type == 'inbound':
            return sum(i['qty'] for i in self.inbound.get(sku, {}).get(fnsku, []) if i['raw_name'] == src_name)
        return 0

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

    def get_other_fnsku_stock(self, sku, current_fnsku):
        total = 0
        if sku in self.stock:
            for f in self.stock[sku]:
                if f != current_fnsku:
                    total += sum(i['qty'] for w in self.stock[sku][f] for i in self.stock[sku][f][w])
        return total

    # --- 增加 entity_usage 收集发货主体的真实名称 ---
    def execute_deduction(self, sku, target_fnsku, qty_needed, strategy_chain, mode='strict_only'):
        qty_remain = qty_needed
        process_details = {'raw_wh': [], 'zone': [], 'fnsku': [], 'qty': 0}
        deduction_log = []
        usage_breakdown = {}
        entity_usage = {}
        
        for src_type, src_name in strategy_chain:
            if qty_remain <= 0: break
            step_taken = 0
            
            if src_type == 'stock' and sku in self.stock:
                if mode in ['mixed', 'strict_only']:
                    if target_fnsku in self.stock[sku]:
                        for item in self.stock[sku][target_fnsku].get(src_name, []):
                            if qty_remain <= 0: break
                            if item['qty'] <= 0: continue
                            take = min(item['qty'], qty_remain)
                            item['qty'] -= take; qty_remain -= take; step_taken += take
                            entity_usage[item['raw_name']] = entity_usage.get(item['raw_name'], 0) + take
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
                                entity_usage[item['raw_name']] = entity_usage.get(item['raw_name'], 0) + take
                                process_details['raw_wh'].append(item['raw_name'])
                                process_details['zone'].append(item['zone'])
                                process_details['fnsku'].append(other_f)
                                process_details['qty'] += take
                                deduction_log.append(f"{src_name}(加工,-{to_int(take)})")

            elif src_type == 'inbound' and sku in self.inbound:
                if mode == 'strict_only':
                    if target_fnsku in self.inbound[sku]:
                        for item in self.inbound[sku][target_fnsku]:
                            if item['raw_name'] != src_name: continue
                            if qty_remain <= 0: break
                            if item['qty'] <= 0: continue
                            take = min(item['qty'], qty_remain)
                            item['qty'] -= take; qty_remain -= take; step_taken += take
                            entity_usage[item['raw_name']] = entity_usage.get(item['raw_name'], 0) + take
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
                            entity_usage[item['raw_name']] = entity_usage.get(item['raw_name'], 0) + take
                            process_details['raw_wh'].append(src_name)
                            process_details['zone'].append('-')
                            process_details['fnsku'].append(other_f)
                            process_details['qty'] += take
                            deduction_log.append(f"{src_name}兜底加工(-{to_int(take)})")
            
            if step_taken > 0:
                usage_breakdown[src_name] = usage_breakdown.get(src_name, 0) + step_taken

        return qty_remain, usage_breakdown, process_details, deduction_log, entity_usage

# ==========================================
# 4. 主逻辑流程 (基于 V35.3 纯正循环逻辑)
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
                "国内库存(深+外+云)": to_int(snap['深仓'] + snap['外协'] + snap['云仓']),
                "提货计划总量": to_int(snap['提货计划']),
                "净PO未入库(已清洗)": to_int(snap['采购订单']),
                "总有效供应": to_int(total_supply),
                "建议补单缺口": to_int(gap)
            })
    df_order_advice = pd.DataFrame(order_list)

    # === Step 1. 任务统筹池 (SJF 小批量优先) ===
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
            'filled': 0, 'usage': {}, 'entity_usage': {}, 'proc': {'raw_wh': [], 'zone': [], 'fnsku': [], 'qty': 0}, 'logs': []
        })

    tasks.sort(key=lambda x: x['qty'])
    results_map = {}
    
    def update_task(t, rem, usage, proc, logs, e_usage):
        step_fill = (t['qty'] - t['filled']) - rem
        t['filled'] += step_fill
        for k, v in usage.items(): t['usage'][k] = t['usage'].get(k, 0) + v
        for k, v in e_usage.items(): t['entity_usage'][k] = t['entity_usage'].get(k, 0) + v
        if logs: t['logs'].extend(logs)
        if proc:
            t['proc']['raw_wh'].extend(proc['raw_wh']); t['proc']['zone'].extend(proc['zone'])
            t['proc']['fnsku'].extend(proc['fnsku']); t['proc']['qty'] += proc['qty']

    # ==========================================
    # === Step 2 & 3: 独立分阶段多轮扫描循环 ===
    # ==========================================
    
    # 🚨 第一轮：US 独享智能防爆仓/防碎单 (严格独立的循环)
    for t in tasks:
        if t['is_us'] and (t['qty'] - t['filled'] > 0):
            us_first_4 = [('stock', '外协'), ('stock', '云仓'), ('inbound', '提货计划'), ('stock', '深仓')]
            us_po = ('inbound', '采购订单')
            
            satisfied_by_first_4 = False
            for stype, sname in us_first_4:
                av_qty = inv_mgr.get_exact_qty(stype, sname, t['sku'], t['fnsku'])
                if av_qty >= t['qty']:
                    r, u, p, l, eu = inv_mgr.execute_deduction(t['sku'], t['fnsku'], t['qty'], [(stype, sname)], 'strict_only')
                    update_task(t, r, u, p, [f"[US防碎单-首选整发]:{x}" for x in l], eu)
                    satisfied_by_first_4 = True
                    break 
                    
            if not satisfied_by_first_4:
                po_qty = inv_mgr.get_exact_qty(us_po[0], us_po[1], t['sku'], t['fnsku'])
                if po_qty >= t['qty']:
                    max_qty, max_node = 0, None
                    for stype, sname in us_first_4:
                        av_qty = inv_mgr.get_exact_qty(stype, sname, t['sku'], t['fnsku'])
                        if av_qty > max_qty:
                            max_qty = av_qty
                            max_node = (stype, sname)
                    
                    if max_qty > 0 and (t['qty'] - max_qty) <= 200:
                        r, u, p, l, eu = inv_mgr.execute_deduction(t['sku'], t['fnsku'], max_qty, [max_node], 'strict_only')
                        update_task(t, r, u, p, [f"[US防爆仓-强锁清空现货]:{x}" for x in l], eu)
                    else:
                        r, u, p, l, eu = inv_mgr.execute_deduction(t['sku'], t['fnsku'], t['qty'], [us_po], 'strict_only')
                        update_task(t, r, u, p, [f"[US防碎单-PO兜底整发]:{x}" for x in l], eu)

    # 🏆 第二轮：全局精准刮肉 (严格独立的循环)
    for t in tasks:
        rem = t['qty'] - t['filled']
        if rem > 0:
            strat = [('stock', '外协'), ('stock', '云仓'), ('inbound', '提货计划'), ('inbound', '采购订单'), ('stock', '深仓')] if t['is_us'] else \
                    [('stock', '深仓'), ('stock', '外协'), ('stock', '云仓'), ('inbound', '提货计划'), ('inbound', '采购订单')]
            r, u, p, l, eu = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat, 'strict_only')
            update_task(t, r, u, p, [f"[R1精准刮肉]:{x}" for x in l], eu)

    # 🔄 第三轮：非 US 独享异标借用加工 (严格独立的循环)
    for t in tasks:
        if not t['is_us']:
            rem = t['qty'] - t['filled']
            if rem > 0:
                strat = [('stock', '深仓'), ('stock', '外协'), ('stock', '云仓'), ('inbound', '提货计划')]
                r, u, p, l, eu = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat, 'process_only')
                update_task(t, r, u, p, [f"[R2非US异标加工]:{x}" for x in l], eu)

    # 🛟 第四轮：全局净 PO 兜底盲配 (严格独立的循环)
    for t in tasks:
        rem = t['qty'] - t['filled']
        if rem > 0:
            strat = [('inbound', '采购订单')]
            r, u, p, l, eu = inv_mgr.execute_deduction(t['sku'], t['fnsku'], rem, strat, 'process_only')
            update_task(t, r, u, p, [f"[R3净PO兜底盲配]:{x}" for x in l], eu)

    # 📊 第五轮：汇总运算日志
    for t in tasks:
        if t['filled'] < t['qty']: t['logs'].append(f"缺口 {to_int(t['qty'] - t['filled'])}")
        results_map[t['row_idx']] = t
        calc_logs.append({"属性": "US" if t['is_us'] else "非US", "SKU": t['sku'], "FNSKU": t['fnsku'], "需求数": t['qty'], "执行过程": " | ".join(t['logs'])})

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
            
            # --- 精准的外协/云仓调拨数量提取 (非US) ---
            waixie_transfer_qty = to_int(t['usage'].get('外协', 0) + t['usage'].get('云仓', 0)) if not t['is_us'] else 0

            # --- 真实发货主体提取拼接 ---
            entity_parts = [f"{k}({to_int(v)})" for k, v in t['entity_usage'].items() if v > 0]
            entity_str = " + ".join(entity_parts) if entity_parts else "-"

            p_wh = "; ".join(list(set(t['proc']['raw_wh'])))
            p_zone = "; ".join(list(set(t['proc']['zone'])))
            p_fn = "; ".join(list(set(t['proc']['fnsku'])))
            p_qt = to_int(t['proc']['qty']) if t['proc']['qty'] > 0 else ""
            
            snap = inv_mgr.get_snapshot(t['sku'])
            total_short = sku_shortage_map.get(t['sku'], 0)
            short_stat = f"❌ 缺货 (该SKU总缺 {to_int(total_short)})" if total_short > 0 else "✅ 全满足"
            
            other_fnsku_stock = inv_mgr.get_other_fnsku_stock(t['sku'], t['fnsku'])
            backup_eye = f"全网剩余 {to_int(other_fnsku_stock)} 个(需撕标)" if other_fnsku_stock > 0 else "无后备现货"

            out_row.update({
                "发货主体": entity_str,
                "库存状态": status_str,
                "最终发货数量": to_int(t['filled']),
                "采购订单数量": to_int(t['usage'].get('采购订单', 0)), 
                "需调回深仓数量(外协/云仓)": waixie_transfer_qty,
                "调拨提示": transfer_note,
                "同SKU其他现货参考(防万一)": backup_eye,
                "缺货与否": short_stat,
                "加工库区": p_wh, "加工库区_库位": p_zone, "加工FNSKU": p_fn, "加工数量": p_qt,
                "剩_深仓": to_int(snap['深仓']), "剩_外协": to_int(snap['外协']),
                "剩_云仓": to_int(snap['云仓']), "剩_计划": to_int(snap['提货计划']), "剩_净PO": to_int(snap['采购订单'])
            })
        else:
             out_row.update({"发货主体": "-", "库存状态": "-", "最终发货数量": 0, "采购订单数量": 0, "需调回深仓数量(外协/云仓)": 0, "调拨提示": "", "同SKU其他现货参考(防万一)": "-", "缺货与否": "-"})
        output_rows.append(out_row)

    return pd.DataFrame(output_rows), calc_logs, inv_mgr.cleaning_logs, df_order_advice

# ==========================================
# 5. UI 渲染
# ==========================================
if 'df_demand' not in st.session_state:
    st.session_state.df_demand = pd.DataFrame(columns=["标签", "国家", "SKU", "FNSKU", "数量", "运营", "店铺", "备注"])

col_main, col_side = st.columns([75, 25])

with col_main:
    st.subheader("1. 需求填报 (V35.6 基座溯源版)")
    edited_df = st.data_editor(st.session_state.df_demand, num_rows="dynamic", use_container_width=True, height=400)
    
    cols = list(edited_df.columns)
    def get_idx(cands):
        for i, c in enumerate(cols):
            if c in cands: return i
        return 0

    st.write("🔧 **列映射配置**")
    c1, c2, c3, c4, c5 = st.columns(5)
    map_tag = c1.selectbox("标签列", cols, index=get_idx(['标签']))
    map_country = c2.selectbox("国家列
