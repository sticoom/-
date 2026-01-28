import streamlit as st
import pandas as pd
import io
import copy
import re

# ==========================================
# 1. 基础配置
# ==========================================
st.set_page_config(page_title="智能调拨系统 V17.0 (双周增量版)", layout="wide", page_icon="🦁")

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
st.title("🦁 智能库存分配 V17.0 (第三周占用/增量 + 第四周)")

# ==========================================
# 2. 数据清洗与辅助工具
# ==========================================
def clean_number(x):
    """强制清洗为数字"""
    if pd.isna(x): return 0
    s = str(x).strip().replace(',', '').replace(' ', '')
    try: return float(s)
    except: return 0

def to_int(x):
    """安全转整数"""
    try: return int(round(float(x)))
    except: return 0

def load_and_find_header(file, type_tag):
    """自动寻找表头"""
    if not file: return None, "未上传"
    try:
        file.seek(0)
        if file.name.endswith('.csv'):
            try: df = pd.read_csv(file, header=None, nrows=20, encoding='utf-8-sig')
            except: 
                file.seek(0)
                df = pd.read_csv(file, header=None, nrows=20, encoding='gbk')
        else:
            df = pd.read_excel(file, header=None, nrows=20)
        
        header_idx = -1
        for i, row in df.iterrows():
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
        self.orig_stock = {} # 原始快照
        
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
        if any(x in n for x in ["亚马逊深圳仓", "深仓"]): return "深仓"
        if any(x in n for x in ["亚马逊外协", "外协"]): return "外协"
        if any(x in n for x in ["云仓", "天源"]): return "云仓"
        return "其他"

    def _init_inventory(self, df):
        self.stats['inv_rows'] = len(df)
        for _, row in df.iterrows():
            s = str(row.get('SKU', '')).strip()
            f_raw = row.get('FNSKU', '')
            f = str(f_raw).strip() if pd.notna(f_raw) else ""
            w_name = str(row.get('仓库名称', ''))
            
            # 过滤黑名单
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
        block_list = ["陈丹丹", "张萍", "杨上儒", "陈炜填", "贝少婷", "詹翠萍"]
        
        for _, row in df.iterrows():
            s = str(row.get('SKU', '')).strip()
            if col_req:
                req = str(row.get(col_req, ''))
                if any(n in req for n in block_list):
                    self.stats['filtered_po'] += 1
                    continue

            q = clean_number(row.get('未入库量', 0))
            if q > 0 and s:
                self.po[s] = self.po.get(s, 0) + q
                self.stats['total_po'] += q

    def get_sku_snapshot(self, sku):
        res = {'外协': 0, '云仓': 0, '深仓': 0, 'PO': 0}
        if sku in self.stock:
            for f in self.stock[sku]:
                for w in ['外协', '云仓', '深仓']:
                    res[w] += self.stock[sku][f].get(w, 0)
        res['PO'] = self.po.get(sku, 0)
        return res

    def check_single_wh_availability(self, sku, target_fnsku, wh_type):
        """检查某个单一仓库是否能满足需求 (用于US优先整库)"""
        total = 0
        if sku in self.stock:
            # 同 FNSKU
            if target_fnsku in self.stock[sku]:
                total += self.stock[sku][target_fnsku].get(wh_type, 0)
            # 异 FNSKU (加工)
            for f in self.stock[sku]:
                if f != target_fnsku:
                    total += self.stock[sku][f].get(wh_type, 0)
        return total

    def execute_deduction(self, sku, target_fnsku, qty_needed, strategy_chain):
        """执行库存扣减"""
        qty_remain = qty_needed
        used_sources = []
        process_details = {'wh': [], 'fnsku': [], 'qty': 0}
        
        for src_type, src_name in strategy_chain:
            if qty_remain <= 0: break
            
            # --- STOCK 扣减 ---
            if src_type == 'stock':
                step_val = 0
                # 1. 优先扣减同FNSKU
                if sku in self.stock and target_fnsku in self.stock[sku]:
                    avail = self.stock[sku][target_fnsku].get(src_name, 0)
                    take = min(avail, qty_remain)
                    if take > 0:
                        self.stock[sku][target_fnsku][src_name] -= take
                        qty_remain -= take
                        step_val += take
                
                # 2. 扣减其他FNSKU (加工)
                if qty_remain > 0 and sku in self.stock:
                    for f in self.stock[sku]:
                        if f == target_fnsku: continue
                        if qty_remain <= 0: break
                        avail = self.stock[sku][f].get(src_name, 0)
                        take = min(avail, qty_remain)
                        if take > 0:
                            self.stock[sku][f][src_name] -= take
                            qty_remain -= take
                            step_val += take
                            # 记录加工
                            process_details['wh'].append(src_name)
                            process_details['fnsku'].append(f)
                            process_details['qty'] += take
                
                if step_val > 0 and src_name not in used_sources:
                    used_sources.append(src_name)

            # --- PO 扣减 ---
            elif src_type == 'po':
                if sku in self.po:
                    avail = self.po[sku]
                    take = min(avail, qty_remain)
                    if take > 0:
                        self.po[sku] -= take
                        qty_remain -= take
                        if '采购订单' not in used_sources: used_sources.append('采购订单')

        filled = qty_needed - qty_remain
        return filled, used_sources, process_details

# ==========================================
# 4. 逻辑核心 (策略与分配)
# ==========================================

def parse_orig_status_to_strategy(status_str):
    """解析 '第三周发货原始状态' 文本，生成 T0 优先策略"""
    s = str(status_str).strip()
    # 简单的关键词匹配
    priority = []
    if "深仓" in s: priority.append(('stock', '深仓'))
    if "云仓" in s: priority.append(('stock', '云仓'))
    if "外协" in s: priority.append(('stock', '外协'))
    if "采购" in s or "PO" in s: priority.append(('po', '采购订单'))
    return priority

def get_strategy(country, inv_mgr, sku, fnsku, qty):
    """根据 US/Non-US 生成扣减顺序"""
    c = str(country).upper()
    is_us = 'US' in c or '美国' in c
    
    base_strat = []
    
    if not is_us:
        # Non-US: 深仓 > 云仓 > 外协 > PO
        base_strat = [
            ('stock', '深仓'), ('stock', '云仓'), ('stock', '外协'), ('po', '采购订单')
        ]
    else:
        # US: 外协 > 云仓 > 深仓 > PO
        # 特殊逻辑: 优先以整个库区满足
        candidates = ['外协', '云仓', '深仓']
        best_single = None
        
        # 1. 检查是否有单一仓库能全满足
        for wh in candidates:
            avail = inv_mgr.check_single_wh_availability(sku, fnsku, wh)
            if avail >= qty:
                best_single = wh
                break # 找到了优先的
        
        if best_single:
            # 如果找到了单一满足的，把它放到第一位，其他按默认顺序
            base_strat.append(('stock', best_single))
            for wh in candidates:
                if wh != best_single: base_strat.append(('stock', wh))
            base_strat.append(('po', '采购订单'))
        else:
            # 没找到单一满足的，走默认拼凑: 外协 > 云仓 > 深仓
            base_strat = [
                ('stock', '外协'), ('stock', '云仓'), ('stock', '深仓'), ('po', '采购订单')
            ]
            
    return base_strat

def run_process_v17(df_input, inv_mgr, df_plan):
    
    # ------------------------------------
    # 0. 提货计划预处理 (Tier -1)
    # ------------------------------------
    if df_plan is not None and not df_plan.empty:
        p_sku = smart_col(df_plan, ['SKU'])
        p_qty = smart_col(df_plan, ['数量', '需求', '计划'])
        if p_sku and p_qty:
            for _, row in df_plan.iterrows():
                sku = str(row[p_sku]).strip()
                qty = clean_number(row[p_qty])
                if qty > 0:
                    # 计划表这里简化处理，默认走Non-US逻辑扣库存
                    strat = [('stock', '深仓'), ('stock', '云仓'), ('stock', '外协')]
                    inv_mgr.execute_deduction(sku, "", qty, strat)

    # ------------------------------------
    # 1. 解析任务 (Task Splitting)
    # ------------------------------------
    tasks = []
    
    # 必选列映射
    c_sku = smart_col(df_input, ['SKU'])
    c_fnsku = smart_col(df_input, ['FNSKU'])
    c_country = smart_col(df_input, ['国家', 'Country'])
    
    # 第三周
    c_w3_orig = smart_col(df_input, ['第三周发货原始数量'])
    c_w3_stat = smart_col(df_input, ['第三周发货原始状态'])
    c_w3_final = smart_col(df_input, ['第三周发货最终数量'])
    
    # 第四周
    c_w4_orig = smart_col(df_input, ['第四周发货原始数量']) # 也即第四周需求
    
    # 辅助列
    c_tag = smart_col(df_input, ['标签'])
    
    if not (c_sku and c_w3_orig and c_w3_final and c_w4_orig):
        return pd.DataFrame() # 缺少关键列
    
    for idx, row in df_input.iterrows():
        sku = str(row[c_sku]).strip()
        fnsku = str(row.get(c_fnsku, '')).strip()
        country = str(row.get(c_country, '')).strip()
        is_us = 'US' in country.upper() or '美国' in country.upper()
        
        # --- Task A: 第三周原始占用 (Tier 0) ---
        w3_orig_qty = clean_number(row.get(c_w3_orig, 0))
        w3_orig_status_text = str(row.get(c_w3_stat, '')) if c_w3_stat else ""
        
        if w3_orig_qty > 0:
            tasks.append({
                'id': idx, 'type': 'W3_Base', 'prio': 0,
                'sku': sku, 'fnsku': fnsku, 'country': country,
                'qty': w3_orig_qty, 'pref_wh': w3_orig_status_text
            })
            
        # --- Task B: 第三周增量 (Tier 1/2) ---
        w3_final_qty = clean_number(row.get(c_w3_final, 0))
        incr_qty = w3_final_qty - w3_orig_qty
        
        if incr_qty > 0:
            p = 2 if is_us else 1
            tasks.append({
                'id': idx, 'type': 'W3_Incr', 'prio': p,
                'sku': sku, 'fnsku': fnsku, 'country': country,
                'qty': incr_qty
            })
            
        # --- Task C: 第四周全量 (Tier 3/4) ---
        w4_qty = clean_number(row.get(c_w4_orig, 0))
        if w4_qty > 0:
            p = 4 if is_us else 3
            tasks.append({
                'id': idx, 'type': 'W4', 'prio': p,
                'sku': sku, 'fnsku': fnsku, 'country': country,
                'qty': w4_qty
            })

    # ------------------------------------
    # 2. 执行分配 (按优先级排序)
    # ------------------------------------
    tasks.sort(key=lambda x: x['prio'])
    
    # 结果暂存: map[row_idx] -> { 'w3_fill':..., 'w3_src':..., ... }
    results = {}
    
    for t in tasks:
        rid = t['id']
        if rid not in results:
            results[rid] = {
                'w3_base_fill': 0, 'w3_base_src': [],
                'w3_incr_fill': 0, 'w3_incr_src': [],
                'w4_fill': 0, 'w4_src': [],
                
                # 加工明细分开存
                'w3_proc_fnsku': [], 'w3_proc_wh': [], 'w3_proc_qty': 0,
                'w4_proc_fnsku': [], 'w4_proc_wh': [], 'w4_proc_qty': 0
            }
            
        qty = t['qty']
        
        # 确定策略
        strat = []
        if t['type'] == 'W3_Base':
            # T0: 尝试优先使用原始状态指明的仓库
            pref = parse_orig_status_to_strategy(t['pref_wh'])
            if pref:
                # 如果有指定，优先用指定的，剩下的走默认 Non-US 逻辑(深仓优先)
                strat = pref + [('stock', '深仓'), ('stock', '云仓'), ('stock', '外协'), ('po', '采购订单')]
                # 去重
                seen = set()
                final_strat = []
                for x in strat:
                    if x not in seen:
                        final_strat.append(x)
                        seen.add(x)
                strat = final_strat
            else:
                # 没指定，默认 Non-US 逻辑
                strat = [('stock', '深仓'), ('stock', '云仓'), ('stock', '外协'), ('po', '采购订单')]
        else:
            # T1-T4: 走标准区域策略
            strat = get_strategy(t['country'], inv_mgr, t['sku'], t['fnsku'], qty)
            
        # 执行
        filled, srcs, proc = inv_mgr.execute_deduction(t['sku'], t['fnsku'], qty, strat)
        
        # 回填数据
        r = results[rid]
        if t['type'] == 'W3_Base':
            r['w3_base_fill'] += filled
            r['w3_base_src'].extend(srcs)
            r['w3_proc_fnsku'].extend(proc['fnsku'])
            r['w3_proc_wh'].extend(proc['wh'])
            r['w3_proc_qty'] += proc['qty']
        elif t['type'] == 'W3_Incr':
            r['w3_incr_fill'] += filled
            r['w3_incr_src'].extend(srcs)
            r['w3_proc_fnsku'].extend(proc['fnsku'])
            r['w3_proc_wh'].extend(proc['wh'])
            r['w3_proc_qty'] += proc['qty']
        elif t['type'] == 'W4':
            r['w4_fill'] += filled
            r['w4_src'].extend(srcs)
            r['w4_proc_fnsku'].extend(proc['fnsku'])
            r['w4_proc_wh'].extend(proc['wh'])
            r['w4_proc_qty'] += proc['qty']

    # ------------------------------------
    # 3. 结果聚合输出
    # ------------------------------------
    output_rows = []
    
    # 辅助函数: 列表去重转字符串
    def fmt_list(lst):
        return "+".join(sorted(list(set(lst)))) if lst else ""
    
    def fmt_proc(lst):
        return ";".join([str(x) for x in lst]) if lst else ""

    for idx, row in df_input.iterrows():
        # 读取原始信息
        res = {
            "国家": row.get(c_country, ''),
            "SKU": row.get(c_sku, ''),
            "FNSKU": row.get(c_fnsku, ''),
            "第三周发货原始数量": to_int(row.get(c_w3_orig, 0)),
            "第三周发货原始状态": row.get(c_w3_stat, ''),
            "第三周发货最终数量": to_int(row.get(c_w3_final, 0)),
            "第四周发货原始数量": to_int(row.get(c_w4_orig, 0)),
            
            # 辅助
            "运营": row.get(smart_col(df_input, ['运营']), ''),
            "店铺": row.get(smart_col(df_input, ['店铺']), ''),
            "备注": row.get(smart_col(df_input, ['备注']), ''),
            # 排序辅助
            "Tag": row.get(c_tag, '')
        }
        
        # 填充计算结果
        if idx in results:
            d = results[idx]
            
            # W3 汇总
            w3_total_fill = d['w3_base_fill'] + d['w3_incr_fill']
            w3_need = res['第三周发货最终数量'] # 即 Orig + Incr
            
            # W3 状态
            w3_base_s = fmt_list(d['w3_base_src'])
            w3_incr_s = fmt_list(d['w3_incr_src'])
            if w3_incr_s:
                w3_status_str = f"[原:{w3_base_s}] + [增:{w3_incr_s}]"
            else:
                w3_status_str = w3_base_s if w3_base_s else "无"
            
            # W3 加工
            res['第三周发货最终状态'] = w3_status_str
            res['第三周发货需加工FNSKU'] = fmt_proc(d['w3_proc_fnsku'])
            res['加工库区'] = fmt_proc(d['w3_proc_wh'])
            res['加工数量'] = to_int(d['w3_proc_qty']) if d['w3_proc_qty'] else ""
            
            # W3 对比
            res['第三周需求对比(原->新)'] = f"{res['第三周发货原始数量']} -> {res['第三周发货最终数量']}"
            
            # W4 汇总
            w4_need = res['第四周发货原始数量']
            w4_fill = d['w4_fill']
            res['第四周发货最终状态'] = fmt_list(d['w4_src']) if d['w4_src'] else ("缺货" if w4_need>0 else "-")
            
            # W4 加工 (复用列名或新增? 根据Prompt "具体放到对应列中去")
            # 这里我新增专属于W4的加工列，避免混淆
            res['第四周发货需加工FNSKU'] = fmt_proc(d['w4_proc_fnsku'])
            res['第四周加工库区'] = fmt_proc(d['w4_proc_wh'])
            res['第四周加工数量'] = to_int(d['w4_proc_qty']) if d['w4_proc_qty'] else ""
            
            # 整体核心状态
            total_shortage = (w3_need + w4_need) - (w3_total_fill + w4_fill)
            res['最终发货数量'] = to_int(w3_total_fill + w4_fill)
            res['发货对比(原->终)'] = f"{to_int(d['w3_base_fill'])} -> {res['最终发货数量']}"
            
            if total_shortage <= 0.001:
                res['是否全满足'] = "✅ 全满足"
            else:
                res['是否全满足'] = f"❌ 不满足 (缺{to_int(total_shortage)})"
                
            res['订单状态'] = f"W3:{w3_status_str} | W4:{res['第四周发货最终状态']}"

            # 剩余快照
            snap = inv_mgr.get_sku_snapshot(res['SKU'])
            res['剩_深仓'] = to_int(snap['深仓'])
            res['剩_外协'] = to_int(snap['外协'])
            res['剩_云仓'] = to_int(snap['云仓'])
            res['剩_PO'] = to_int(snap['PO'])
            
        else:
            # 无需求行
            for k in ['第三周发货最终状态','第三周发货需加工FNSKU','加工库区','加工数量','第三周需求对比(原->新)',
                      '第四周发货最终状态','第四周发货需加工FNSKU','第四周加工库区','第四周加工数量',
                      '最终发货数量','发货对比(原->终)','是否全满足','订单状态',
                      '剩_深仓','剩_外协','剩_云仓','剩_PO']:
                res[k] = ""
                
        output_rows.append(res)
        
    # ------------------------------------
    # 4. 排序与格式化
    # ------------------------------------
    df_out = pd.DataFrame(output_rows)
    if df_out.empty: return df_out
    
    # 排序: SKU -> Tag(新增在前) -> Country(非US在前)
    df_out['p_tag'] = df_out['Tag'].apply(lambda x: 0 if '新增' in str(x) else 1)
    df_out['p_cty'] = df_out['国家'].apply(lambda x: 1 if 'US' in str(x).upper() else 0)
    
    df_out = df_out.sort_values(by=['SKU', 'p_tag', 'p_cty'])
    
    # 最终列筛选与顺序
    final_cols = [
        "国家", "SKU", "FNSKU", 
        "第三周发货原始数量", "第三周发货原始状态", "第三周发货最终数量", "第三周发货最终状态",
        "第三周发货需加工FNSKU", "加工库区", "加工数量",
        "第四周发货原始数量", "第四周发货最终状态", 
        "第四周发货需加工FNSKU", "第四周加工库区", "第四周加工数量",
        "第三周需求对比(原->新)", "最终发货数量", "发货对比(原->终)", 
        "是否全满足", "库存分配状态对比", "订单状态",
        "剩_深仓", "剩_外协", "剩_云仓", "剩_PO",
        "运营", "店铺", "备注"
    ]
    # 仅保留存在的列
    cols = [c for c in final_cols if c in df_out.columns]
    return df_out[cols]

# ==========================================
# 5. UI 主程序
# ==========================================
col1, col2 = st.columns([30, 70])

with col1:
    st.header("1. 需求表上传")
    st.info("💡 必须包含：标签, 国家, SKU, FNSKU, 第三周发货原始数量, 第三周发货原始状态, 第三周发货最终数量, 第四周发货原始数量")
    f_demand = st.file_uploader("📤 上传需求", type=['xlsx', 'csv'])
    
with col2:
    st.header("2. 库存与设置")
    c1, c2, c3 = st.columns(3)
    f_inv = c1.file_uploader("A. 库存表", type=['xlsx', 'csv'])
    f_po = c2.file_uploader("B. PO表", type=['xlsx', 'csv'])
    f_plan = c3.file_uploader("C. 计划表(可选)", type=['xlsx', 'csv'])
    
    if st.button("🚀 运行 V17.0 计算", type="primary", use_container_width=True):
        if f_demand and f_inv and f_po:
            try:
                # Load
                df_d, _ = load_and_find_header(f_demand, "需求")
                df_i, _ = load_and_find_header(f_inv, "库存")
                df_p, _ = load_and_find_header(f_po, "PO")
                df_plan_raw = None
                if f_plan: df_plan_raw, _ = load_and_find_header(f_plan, "计划")
                
                # Init Manager
                # Map columns manually to be safe
                i_map = {smart_col(df_i,['SKU']):'SKU', smart_col(df_i,['FNSKU']):'FNSKU', 
                         smart_col(df_i,['仓库']):'仓库名称', smart_col(df_i,['可用']):'可用库存'}
                p_map = {smart_col(df_p,['SKU']):'SKU', smart_col(df_p,['未入库']):'未入库量'}
                
                mgr = InventoryManager(df_i.rename(columns=i_map), df_p.rename(columns=p_map))
                
                # Run
                res_df = run_process_v17(df_d, mgr, df_plan_raw)
                
                if not res_df.empty:
                    # Highlight
                    def highlight_fail(row):
                        return ['background-color: #ffcdd2' if '不满足' in str(row['是否全满足']) else '' for _ in row]
                    
                    st.write("### ✅ 分配结果")
                    st.dataframe(res_df.style.apply(highlight_fail, axis=1), use_container_width=True)
                    
                    # Download
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                        res_df.to_excel(writer, index=False, sheet_name='Result')
                        writer.sheets['Result'].freeze_panes(1, 0)
                    
                    st.download_button("📥 下载结果 Excel", buf.getvalue(), "V17_Result.xlsx")
                else:
                    st.error("计算结果为空，请检查输入列名是否匹配")
                    
            except Exception as e:
                st.error(f"发生错误: {e}")
                st.exception(e)
        else:
            st.warning("请上传必要文件 (需求、库存、PO)")
