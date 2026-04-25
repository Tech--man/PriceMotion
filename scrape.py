#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国内大模型 API 价格抓取脚本
每天自动运行一次，生成 models.json 供前端使用
"""

import json
import re
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict

from playwright.sync_api import sync_playwright, Browser, Page

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SCRIPT_DIR = Path(__file__).parent
PROVIDERS_FILE = SCRIPT_DIR / "providers.json"
MODELS_FILE = SCRIPT_DIR / "models.json"

NOW = datetime.now(timezone.utc).isoformat()


# ── 公用工具 ────────────────────────────────────

def parse_price(text: str) -> float:
    """从字符串中提取价格（元/百万tokens），失败返回 -1"""
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", "").replace("，", "").strip())
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return -1


def slugify(name: str, provider: str) -> str:
    """生成模型 id：provider + 简化名称"""
    s = name.lower().strip()
    s = re.sub(r"[\s\-]+", "-", s)
    s = re.sub(r"[^a-z0-9\-]", "", s)
    s = s.strip("-")
    return f"{provider}-{s}" if not s.startswith(provider) else s


def safe_inner_text(element, default: str = "") -> str:
    """安全获取元素文本"""
    try:
        return element.inner_text().strip()
    except Exception:
        return default


def load_providers() -> Dict:
    """加载 providers.json"""
    try:
        with open(PROVIDERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logging.error(f"providers.json 不存在: {PROVIDERS_FILE}")
        return {"providers": {}, "models": {}}


def load_previous_models() -> List[Dict]:
    """加载上一次的 models.json"""
    try:
        with open(MODELS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def merge_models(
    scraped: List[Dict],
    providers: Dict,
    previous: List[Dict],
) -> List[Dict]:
    """合并爬取数据、编辑元数据和旧数据"""
    old_by_id = {m["id"]: m for m in previous}
    result = {}

    provider_map = providers.get("providers", {})
    model_meta = providers.get("models", {})

    for m in scraped:
        mid = m["id"]
        # 过滤掉无效模型：id 为空、价格未解析
        if not mid or mid.endswith("-") or m.get("inputPrice", -1) < 0:
            logging.debug(f"跳过无效模型: {mid} (inputPrice={m.get('inputPrice')})")
            continue
        # 找到对应的 provider key
        provider_key = mid.split("-")[0] if "-" in mid else ""
        p_info = provider_map.get(provider_key, {})

        # 找到对应的编辑元数据
        meta = model_meta.get(mid, {})
        if mid not in model_meta:
            logging.warning(f"新模型 {mid} 不在 providers.json 中，需要补充元数据")

        merged = {
            "id": mid,
            "company": p_info.get("company", m.get("company", "")),
            "companyColor": p_info.get("companyColor", m.get("companyColor", "")),
            "model": m.get("model", mid),
            "desc": meta.get("desc", ""),
            "context": meta.get("context", m.get("context", "")),
            "inputPrice": m.get("inputPrice", -1),
            "outputPrice": m.get("outputPrice", -1),
            "tags": meta.get("tags", []),
            "url": m.get("url", p_info.get("url", "")),
            "note": meta.get("note", ""),
            "lastUpdated": m.get("lastUpdated", NOW),
        }
        result[mid] = merged

    # 保留旧数据中未被本次爬取到的模型
    for m in previous:
        if m["id"] not in result:
            result[m["id"]] = m

    return sorted(result.values(), key=lambda x: (x.get("company", ""), x.get("id", "")))


def extract_table_models(
    page: Page,
    provider_key: str,
    url: str,
    name_col: int = 0,
    input_col: int = 1,
    output_col: int = 2,
    context_col: Optional[int] = None,
    table_selector: str = "table",
    wait_selector: str = "table",
) -> List[Dict]:
    """通用表格解析器：从页面表格中提取模型数据"""
    models = []
    try:
        page.wait_for_selector(wait_selector, timeout=10000)
        rows = page.query_selector_all(f"{table_selector} tr")
        if not rows:
            return []
        for row in rows[1:]:
            cols = row.query_selector_all("td")
            if len(cols) < max(name_col, input_col, output_col) + 1:
                continue
            model_name = safe_inner_text(cols[name_col])
            if not model_name:
                continue
            input_price = parse_price(safe_inner_text(cols[input_col]))
            output_price = parse_price(safe_inner_text(cols[output_col]))
            context = ""
            if context_col is not None and len(cols) > context_col:
                context = safe_inner_text(cols[context_col])
            mid = slugify(model_name, provider_key)
            models.append({
                "id": mid,
                "model": model_name,
                "inputPrice": input_price,
                "outputPrice": output_price,
                "url": url,
                "context": context,
                "lastUpdated": NOW,
            })
    except Exception as e:
        logging.error(f"表格解析失败 ({provider_key}): {e}")
    return models


def create_page(browser: Browser, url: str, wait_selector: str = "body", timeout: int = 20000) -> Optional[Page]:
    """创建页面并导航到 URL，失败返回 None"""
    page = None
    try:
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        page.wait_for_selector(wait_selector, timeout=15000)
        return page
    except Exception as e:
        logging.error(f"页面加载失败 {url}: {e}")
        if page and not page.is_closed():
            page.close()
        return None


# ── 各厂商解析器 ──────────────────────────────────

def scrape_deepseek(browser: Browser) -> List[Dict]:
    """DeepSeek 定价页"""
    url = "https://platform.deepseek.com/api-docs/pricing"
    page = create_page(browser, url, "table, .pricing, [class*='price']")
    if not page:
        return []
    try:
        models = []
        # DeepSeek 通常使用表格展示价格
        rows = page.query_selector_all("table tr")
        if rows:
            for row in rows[1:]:
                cols = row.query_selector_all("td")
                if len(cols) < 3:
                    continue
                model_name = safe_inner_text(cols[0])
                if not model_name:
                    continue
                input_price = parse_price(safe_inner_text(cols[1]))
                output_price = parse_price(safe_inner_text(cols[2]))
                mid = slugify(model_name, "deepseek")
                models.append({
                    "id": mid,
                    "model": model_name,
                    "inputPrice": input_price,
                    "outputPrice": output_price,
                    "url": url,
                    "context": "128K",
                    "lastUpdated": NOW,
                })
        else:
            # 备用：尝试卡片式布局
            cards = page.query_selector_all("[class*='model'], [class*='pricing-card']")
            for card in cards:
                name_el = card.query_selector("[class*='name'], [class*='title'], h3, h4")
                if not name_el:
                    continue
                model_name = safe_inner_text(name_el)
                if not model_name:
                    continue
                price_els = card.query_selector_all("[class*='price'], [class*='cost']")
                input_price = parse_price(safe_inner_text(price_els[0])) if len(price_els) > 0 else -1
                output_price = parse_price(safe_inner_text(price_els[1])) if len(price_els) > 1 else -1
                mid = slugify(model_name, "deepseek")
                models.append({
                    "id": mid,
                    "model": model_name,
                    "inputPrice": input_price,
                    "outputPrice": output_price,
                    "url": url,
                    "context": "128K",
                    "lastUpdated": NOW,
                })
        logging.info(f"DeepSeek: 获取到 {len(models)} 个模型")
        return models
    finally:
        page.close()


def scrape_qwen(browser: Browser) -> List[Dict]:
    """阿里通义千问模型定价"""
    url = "https://help.aliyun.com/zh/model-studio/getting-started/models"
    page = create_page(browser, url, "table, [class*='price']")
    if not page:
        return []
    try:
        models = []
        # 阿里云文档有多个表格，找包含"输入"和"输出"列的表格
        tables = page.query_selector_all("table")
        target = None
        for table in tables:
            text = safe_inner_text(table)
            if "输入" in text and "输出" in text:
                target = table
                break
        if target:
            rows = target.query_selector_all("tr")
            for row in rows[1:]:
                cols = row.query_selector_all("td")
                if len(cols) < 4:
                    continue
                model_name = safe_inner_text(cols[0])
                if not model_name or "embedding" in model_name.lower():
                    continue
                # 寻找输入和输出价格列
                input_price = -1
                output_price = -1
                context = ""
                for i, col in enumerate(cols):
                    text = safe_inner_text(col)
                    header = ""
                    header_row = row.evaluate_handle(
                        "el => el.closest('table').querySelector('tr')", )
                    # 通过表头匹配
                # 简化处理：假设列顺序为 模型名、上下文、输入价格、输出价格
                # 或 模型名、输入价格、输出价格
                if len(cols) >= 5:
                    context = safe_inner_text(cols[1])
                    input_price = parse_price(safe_inner_text(cols[2]))
                    output_price = parse_price(safe_inner_text(cols[3]))
                elif len(cols) >= 3:
                    input_price = parse_price(safe_inner_text(cols[1]))
                    output_price = parse_price(safe_inner_text(cols[2]))
                mid = slugify(model_name, "qwen")
                models.append({
                    "id": mid,
                    "model": model_name,
                    "inputPrice": input_price,
                    "outputPrice": output_price,
                    "url": url,
                    "context": context,
                    "lastUpdated": NOW,
                })
        if not models:
            # 备用：尝试 div 卡片布局
            models = extract_table_models(page, "qwen", url)
        logging.info(f"通义千问: 获取到 {len(models)} 个模型")
        return models
    finally:
        page.close()


def scrape_zhipu(browser: Browser) -> List[Dict]:
    """智谱 AI 定价"""
    url = "https://open.bigmodel.cn/pricing"
    page = create_page(browser, url, "table, .pricing, [class*='price']")
    if not page:
        return []
    try:
        models = []
        # 尝试表格
        table = page.query_selector("table.pricing-table, table")
        if table:
            rows = table.query_selector_all("tr")
            for row in rows[1:]:
                cols = row.query_selector_all("td")
                if len(cols) < 3:
                    continue
                model_name = safe_inner_text(cols[0])
                if not model_name:
                    continue
                input_price = parse_price(safe_inner_text(cols[1]))
                output_price = parse_price(safe_inner_text(cols[2]))
                context = safe_inner_text(cols[3]) if len(cols) > 3 else ""
                mid = slugify(model_name, "glm")
                # 智谱的 id 格式特殊
                if "flash" in model_name.lower():
                    mid = "glm-4-flash"
                elif "air" in model_name.lower():
                    mid = "glm-4-air"
                elif "glm-4" in model_name.lower() and "flash" not in model_name.lower() and "air" not in model_name.lower():
                    mid = "glm-4"
                models.append({
                    "id": mid,
                    "model": model_name,
                    "inputPrice": input_price,
                    "outputPrice": output_price,
                    "url": url,
                    "context": context,
                    "lastUpdated": NOW,
                })
        if not models:
            # 备用：卡片布局
            cards = page.query_selector_all("[class*='pricing-card'], [class*='model-item']")
            for card in cards:
                name_el = card.query_selector("[class*='name'], [class*='title'], h3, h4")
                if not name_el:
                    continue
                model_name = safe_inner_text(name_el)
                price_els = card.query_selector_all("[class*='price']")
                input_price = parse_price(safe_inner_text(price_els[0])) if len(price_els) > 0 else 0
                output_price = parse_price(safe_inner_text(price_els[1])) if len(price_els) > 1 else input_price
                mid = slugify(model_name, "glm")
                models.append({
                    "id": mid,
                    "model": model_name,
                    "inputPrice": input_price,
                    "outputPrice": output_price,
                    "url": url,
                    "context": "",
                    "lastUpdated": NOW,
                })
        logging.info(f"智谱AI: 获取到 {len(models)} 个模型")
        return models
    finally:
        page.close()


def scrape_moonshot(browser: Browser) -> List[Dict]:
    """Kimi 定价"""
    url = "https://platform.moonshot.cn/docs/pricing"
    page = create_page(browser, url, "table")
    if not page:
        return []
    try:
        models = extract_table_models(
            page, "kimi", url,
            name_col=0, input_col=1, output_col=1,
            context_col=2,
        )
        # Moonshot 输入输出同价
        for m in models:
            m["outputPrice"] = m["inputPrice"]
            if "note" not in m:
                m["note"] = ""
        logging.info(f"Kimi: 获取到 {len(models)} 个模型")
        return models
    finally:
        page.close()


def scrape_baichuan(browser: Browser) -> List[Dict]:
    """百川智能定价"""
    url = "https://platform.baichuan-ai.com/price"
    page = create_page(browser, url, "table, .pricing, [class*='price'], [class*='model']")
    if not page:
        return []
    try:
        models = []
        table = page.query_selector("table")
        if table:
            rows = table.query_selector_all("tr")
            for row in rows[1:]:
                cols = row.query_selector_all("td")
                if len(cols) < 3:
                    continue
                model_name = safe_inner_text(cols[0])
                if not model_name:
                    continue
                input_price = parse_price(safe_inner_text(cols[1]))
                output_price = parse_price(safe_inner_text(cols[2]))
                mid = slugify(model_name, "baichuan")
                models.append({
                    "id": mid,
                    "model": model_name,
                    "inputPrice": input_price,
                    "outputPrice": output_price,
                    "url": url,
                    "context": "",
                    "lastUpdated": NOW,
                })
        else:
            # 尝试卡片/列表布局
            items = page.query_selector_all("[class*='price-item'], [class*='model-card'], [class*='model-row']")
            for item in items:
                name_el = item.query_selector("[class*='name'], [class*='title'], h3, h4")
                if not name_el:
                    continue
                model_name = safe_inner_text(name_el)
                price_els = item.query_selector_all("[class*='price']")
                input_price = parse_price(safe_inner_text(price_els[0])) if len(price_els) > 0 else -1
                output_price = parse_price(safe_inner_text(price_els[1])) if len(price_els) > 1 else input_price
                mid = slugify(model_name, "baichuan")
                models.append({
                    "id": mid,
                    "model": model_name,
                    "inputPrice": input_price,
                    "outputPrice": output_price,
                    "url": url,
                    "context": "",
                    "lastUpdated": NOW,
                })
        logging.info(f"百川智能: 获取到 {len(models)} 个模型")
        return models
    finally:
        page.close()


def scrape_xfyun(browser: Browser) -> List[Dict]:
    """讯飞星火定价"""
    url = "https://www.xfyun.cn/doc/spark/Web.html"
    page = create_page(browser, url, "table, [class*='price'], [class*='model']")
    if not page:
        return []
    try:
        models = []
        tables = page.query_selector_all("table")
        for table in tables:
            text = safe_inner_text(table)
            if "价格" not in text and "元" not in text and "免费" not in text:
                continue
            rows = table.query_selector_all("tr")
            for row in rows[1:]:
                cols = row.query_selector_all("td")
                if len(cols) < 3:
                    continue
                model_name = safe_inner_text(cols[0])
                if not model_name or "spark" not in model_name.lower() and "星火" not in model_name:
                    continue
                # 查找价格列
                input_price = -1
                output_price = -1
                for col in cols[1:]:
                    text = safe_inner_text(col)
                    price = parse_price(text)
                    if price >= 0:
                        if input_price < 0:
                            input_price = price
                        elif output_price < 0:
                            output_price = price
                            break
                if "免费" in safe_inner_text(row):
                    input_price = 0
                    output_price = 0
                mid = slugify(model_name, "spark")
                models.append({
                    "id": mid,
                    "model": model_name,
                    "inputPrice": input_price,
                    "outputPrice": output_price if output_price >= 0 else input_price,
                    "url": url,
                    "context": "",
                    "lastUpdated": NOW,
                })
        logging.info(f"讯飞星火: 获取到 {len(models)} 个模型")
        return models
    finally:
        page.close()


def scrape_doubao(browser: Browser) -> List[Dict]:
    """字节豆包定价"""
    url = "https://www.volcengine.com/docs/82379/1099320"
    page = create_page(browser, url, "table, [class*='price']")
    if not page:
        return []
    try:
        models = []
        # 只取包含对话/文本模型定价的表格
        tables = page.query_selector_all("table")
        for table in tables:
            text = safe_inner_text(table)
            if "输入" not in text and "输出" not in text:
                continue
            rows = table.query_selector_all("tr")
            for row in rows[1:]:
                cols = row.query_selector_all("td")
                if len(cols) < 3:
                    continue
                model_name = safe_inner_text(cols[0])
                if not model_name:
                    continue
                # 过滤掉嵌入/向量/重排等非对话模型
                skip_keywords = ["embedding", "embed", "向量", "重排", "rerank"]
                if any(kw in model_name.lower() for kw in skip_keywords):
                    continue
                input_price = parse_price(safe_inner_text(cols[1]))
                output_price = parse_price(safe_inner_text(cols[2]))
                # 跳过价格未解析的行
                if input_price < 0:
                    continue
                mid = slugify(model_name, "doubao")
                models.append({
                    "id": mid,
                    "model": model_name,
                    "inputPrice": input_price,
                    "outputPrice": output_price if output_price >= 0 else input_price,
                    "url": url,
                    "context": "",
                    "lastUpdated": NOW,
                })
        logging.info(f"字节豆包: 获取到 {len(models)} 个模型")
        return models
    finally:
        page.close()


def scrape_hunyuan(browser: Browser) -> List[Dict]:
    """腾讯混元定价"""
    url = "https://cloud.tencent.com/document/product/1729/97731"
    page = create_page(browser, url, "table, .table-box")
    if not page:
        return []
    try:
        models = []
        # 腾讯云文档表格通常在 .table-box 内
        table = page.query_selector(".table-box table") or page.query_selector("table")
        if table:
            rows = table.query_selector_all("tr")
            for row in rows[1:]:
                cols = row.query_selector_all("td")
                if len(cols) < 3:
                    continue
                model_name = safe_inner_text(cols[0])
                if not model_name:
                    continue
                input_price = parse_price(safe_inner_text(cols[1]))
                output_price = parse_price(safe_inner_text(cols[2]))
                if "免费" in safe_inner_text(row):
                    input_price = 0
                    output_price = 0
                mid = slugify(model_name, "hunyuan")
                models.append({
                    "id": mid,
                    "model": model_name,
                    "inputPrice": input_price,
                    "outputPrice": output_price,
                    "url": url,
                    "context": "",
                    "lastUpdated": NOW,
                })
        logging.info(f"腾讯混元: 获取到 {len(models)} 个模型")
        return models
    finally:
        page.close()


def scrape_ernie(browser: Browser) -> List[Dict]:
    """百度文心一言定价"""
    url = "https://cloud.baidu.com/doc/WENXINWORKSHOP/s/hlrk4akp7"
    page = create_page(browser, url, "table, [class*='price']")
    if not page:
        return []
    try:
        models = []
        # 百度文档价格信息分散在多个表格中
        tables = page.query_selector_all("table")
        for table in tables:
            text = safe_inner_text(table)
            if "价格" not in text and "元" not in text and "免费" not in text:
                continue
            rows = table.query_selector_all("tr")
            for row in rows[1:]:
                cols = row.query_selector_all("td")
                if len(cols) < 2:
                    continue
                model_name = safe_inner_text(cols[0])
                if not model_name:
                    continue
                # 百度页面价格列不固定，尝试智能查找
                input_price = -1
                output_price = -1
                is_free = "免费" in safe_inner_text(row)
                if is_free:
                    input_price = 0
                    output_price = 0
                else:
                    for col in cols[1:]:
                        text = safe_inner_text(col)
                        price = parse_price(text)
                        if price >= 0:
                            if input_price < 0:
                                input_price = price
                            elif output_price < 0:
                                output_price = price
                                break
                if input_price < 0 and not is_free:
                    continue
                mid = slugify(model_name, "ernie")
                models.append({
                    "id": mid,
                    "model": model_name,
                    "inputPrice": input_price,
                    "outputPrice": output_price if output_price >= 0 else input_price,
                    "url": url,
                    "context": "",
                    "lastUpdated": NOW,
                })
        logging.info(f"百度文心: 获取到 {len(models)} 个模型")
        return models
    finally:
        page.close()


# ── 汇总与输出 ────────────────────────────────────

def main():
    providers = load_providers()
    previous = load_previous_models()

    scrapers = [
        ("DeepSeek", scrape_deepseek),
        ("阿里通义千问", scrape_qwen),
        ("智谱AI", scrape_zhipu),
        ("月之暗面 Kimi", scrape_moonshot),
        ("百川智能", scrape_baichuan),
        ("讯飞星火", scrape_xfyun),
        ("字节豆包", scrape_doubao),
        ("腾讯混元", scrape_hunyuan),
        ("百度文心", scrape_ernie),
    ]

    all_scraped: List[Dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for name, scraper in scrapers:
            logging.info(f"正在抓取 {name} ...")
            try:
                models = scraper(browser)
                all_scraped.extend(models)
                logging.info(f"√ {name} 获取到 {len(models)} 个模型")
            except Exception as e:
                logging.error(f"× {name} 抓取失败: {e}")
        browser.close()

    # 合并：爬取数据 + 编辑元数据 + 旧数据
    merged = merge_models(all_scraped, providers, previous)

    with open(MODELS_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    logging.info(f"已生成 models.json，共包含 {len(merged)} 个模型")

    # 检查是否有模型未能更新
    fresh = sum(1 for m in merged if m.get("lastUpdated", "").startswith(NOW[:10]))
    stale = len(merged) - fresh
    if stale > 0:
        logging.warning(f"有 {stale} 个模型未能在本次更新（使用旧数据）")


if __name__ == "__main__":
    main()
