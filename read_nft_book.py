#!/usr/bin/env python3
"""
3ook.com NFT Book Downloader + AI 重點整理
給定任一 OpenSea NFT URL，用錢包驗證持有、下載 epub，並可選擇送 Claude 整理重點

用法:
  PRIVATE_KEY=0x... python3 read_nft_book.py <opensea_url>
  PRIVATE_KEY=0x... python3 read_nft_book.py <opensea_url> --summarize
  PRIVATE_KEY=0x... python3 read_nft_book.py <opensea_url> --summarize --summary-only

  # 只整理已下載的 epub（不重新下載）
  PRIVATE_KEY=0x... python3 read_nft_book.py <opensea_url> --summarize --summary-only

  # 需要設定：
  #   PRIVATE_KEY   — 錢包私鑰
  #   ANTHROPIC_API_KEY — Claude API 金鑰（使用 --summarize 時需要）

安全提醒: 使用環境變數傳入私鑰，避免出現在 shell 歷史記錄中
"""

import base64
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
import argparse
import zipfile
from html.parser import HTMLParser

from eth_account import Account
from eth_account.messages import encode_defunct

API_BASE = "https://api.like.co"

# OpenSea chain name → 公開 RPC
CHAIN_RPC = {
    "base":      "https://base-rpc.publicnode.com",
    "ethereum":  "https://ethereum-rpc.publicnode.com",
    "matic":     "https://polygon-bor-rpc.publicnode.com",
    "polygon":   "https://polygon-bor-rpc.publicnode.com",
    "optimism":  "https://optimism-rpc.publicnode.com",
    "arbitrum":  "https://arbitrum-one-rpc.publicnode.com",
}


# ── URL 解析 ──────────────────────────────────────────────────────────────────

def parse_opensea_url(url):
    """
    解析 OpenSea URL，回傳 (chain, contract_address, token_id)
    支援:
      https://opensea.io/assets/<chain>/<contract>/<token_id>
      https://opensea.io/assets/<contract>/<token_id>   (預設 ethereum)
    """
    m = re.search(
        r"opensea\.io/(?:assets|item)/([^/]+)/0x([0-9a-fA-F]{40})/(\d+)", url
    )
    if m:
        chain = m.group(1).lower()
        contract = "0x" + m.group(2)
        token_id = int(m.group(3))
        return chain, contract, token_id

    m = re.search(r"opensea\.io/(?:assets|item)/0x([0-9a-fA-F]{40})/(\d+)", url)
    if m:
        return "ethereum", "0x" + m.group(1), int(m.group(2))

    raise ValueError(f"無法解析 OpenSea URL: {url}\n"
                     "格式應為: https://opensea.io/assets/<chain>/<contract>/<token_id>")


# ── RPC 工具 ──────────────────────────────────────────────────────────────────

def rpc_call(rpc_url, method, params):
    payload = json.dumps({
        "jsonrpc": "2.0", "method": method, "params": params, "id": 1
    }).encode()
    req = urllib.request.Request(
        rpc_url, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    )
    return json.loads(urllib.request.urlopen(req).read())


def get_token_uri(rpc_url, contract, token_id):
    """呼叫 tokenURI(uint256)，回傳 URI 字串"""
    token_hex = hex(token_id)[2:].zfill(64)
    result = rpc_call(rpc_url, "eth_call", [
        {"to": contract,
         "data": f"0xc87b56dd{token_hex}"},
        "latest"
    ])
    raw = result.get("result", "0x")
    if raw in ("0x", "0x" + "0" * 64):
        raise RuntimeError("tokenURI 回傳空值，合約可能不支援 ERC-721 tokenURI")

    hex_str = raw[2:]
    offset = int(hex_str[0:64], 16) * 2
    length = int(hex_str[offset:offset + 64], 16)
    return bytes.fromhex(hex_str[offset + 64:offset + 64 + length * 2]).decode()


def decode_token_metadata(uri):
    """解碼 tokenURI（支援 data:application/json;base64,... 與 https://）"""
    if uri.startswith("data:application/json;base64,"):
        data = uri.split(",", 1)[1]
        return json.loads(base64.b64decode(data + "=="))
    if uri.startswith("data:application/json,"):
        return json.loads(uri.split(",", 1)[1])
    # HTTP URI
    req = urllib.request.Request(uri, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req).read())


def find_class_id_from_metadata(metadata, contract):
    """從 tokenURI metadata 找出 3ook/liker.land 的 class_id"""
    external_url = metadata.get("external_url", "")

    # 3ook.com/store/<address>/...  or  liker.land/nft/class/<address>/...
    m = re.search(r"0x[0-9a-fA-F]{40}", external_url)
    if m:
        return m.group(0).lower()

    # fallback: 用合約地址本身
    return contract.lower()


def find_owned_token(rpc_url, contract, wallet_address, hint_token_id=None):
    """
    查詢此錢包在合約上持有的 Token ID。
    依序嘗試：
      1. ERC-721 Enumerable: tokenOfOwnerByIndex(wallet, 0)
      2. 直接 ownerOf(hint_token_id) 比對（適用不實作 Enumerable 的合約）
    """
    addr = wallet_address[2:].lower().zfill(64)

    # 方法 1: balanceOf + tokenOfOwnerByIndex
    try:
        result = rpc_call(rpc_url, "eth_call", [
            {"to": contract, "data": f"0x70a08231000000000000000000000000{addr}"},
            "latest"
        ])
        raw = result.get("result", "0x")
        balance = int(raw, 16) if raw and raw != "0x" else 0
        if balance > 0:
            result2 = rpc_call(rpc_url, "eth_call", [
                {"to": contract,
                 "data": f"0x2f745c59000000000000000000000000{addr}"
                         f"0000000000000000000000000000000000000000000000000000000000000000"},
                "latest"
            ])
            raw2 = result2.get("result", "0x")
            if raw2 and raw2 != "0x":
                return int(raw2, 16)
    except Exception:
        pass

    # 方法 2: ownerOf(hint_token_id)
    if hint_token_id is not None:
        try:
            token_hex = hex(hint_token_id)[2:].zfill(64)
            result = rpc_call(rpc_url, "eth_call", [
                {"to": contract, "data": f"0x6352211e{token_hex}"},
                "latest"
            ])
            raw = result.get("result", "0x")
            if raw and raw != "0x" and len(raw) >= 66:
                owner = "0x" + raw[-40:]
                if owner.lower() == wallet_address.lower():
                    return hint_token_id
        except Exception:
            pass

    return None


# ── LikeCoin API ──────────────────────────────────────────────────────────────

def api_post(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        API_BASE + path, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    )
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        msg = e.read().decode()
        raise RuntimeError(f"API 錯誤 {e.code}: {msg}") from e


def authorize(private_key):
    """用 personal_sign 取得 LikeCoin JWT"""
    account = Account.from_key(private_key)
    wallet = account.address

    ts = int(time.time() * 1000)
    message = json.dumps({
        "action": "authorize",
        "permissions": ["read:nftbook", "write:nftbook", "write:iscn", "read:iscn"],
        "evmWallet": wallet,
        "ts": ts
    }, separators=(',', ':'))

    signed = account.sign_message(encode_defunct(text=message))
    signature = signed.signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature

    result = api_post("/wallet/authorize", {
        "wallet": wallet,
        "signature": signature,
        "message": message,
        "signMethod": "personal_sign",
        "expiresIn": "1d"
    })
    return wallet, result["token"]


def download_epub(class_id, nft_id, jwt_token, index=0, output_path=None):
    """透過 LikeCoin ebook-cors 端點下載 epub"""
    params = urllib.parse.urlencode({
        "class_id": class_id,
        "nft_id": str(nft_id),
        "index": str(index),
        "custom_message": "0"
    })
    url = f"{API_BASE}/ebook-cors/?{params}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {jwt_token}",
        "Origin": "https://liker.land",
        "User-Agent": "Mozilla/5.0"
    })

    try:
        response = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        msg = e.read().decode()
        raise RuntimeError(f"下載失敗 {e.code}: {msg}") from e

    total = int(response.headers.get("x-original-content-length")
                or response.headers.get("content-length") or 0)

    if output_path is None:
        output_path = f"nft_book_{class_id[-8:]}_{nft_id}.epub"  # fallback，由呼叫方覆蓋

    downloaded = 0
    with open(output_path, "wb") as f:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = downloaded * 100 // total
                mb = downloaded / 1024 / 1024
                total_mb = total / 1024 / 1024
                print(f"\r  下載中: {pct}%  {mb:.1f} / {total_mb:.1f} MB",
                      end="", flush=True)

    print()
    return output_path


# ── epub 文字擷取 ─────────────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    """把 XHTML 轉為純文字，保留段落與標題結構"""
    SKIP = {'script', 'style', 'head'}
    BLOCK = {'p', 'div', 'li', 'br', 'tr'}
    HEADING = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in self.SKIP:
            self._skip = True
        elif t in self.HEADING:
            self.parts.append('\n\n### ')
        elif t in self.BLOCK:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in self.SKIP:
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self.parts.append(text + ' ')

    def get_text(self):
        return ''.join(self.parts)


def extract_epub_text(epub_path, max_chars=300_000):
    """
    從 epub 解壓並擷取純文字，依 spine 順序合併各章節。
    max_chars 限制最大字元數（約 150K 繁體字 ≈ 200K tokens）。
    """
    with zipfile.ZipFile(epub_path, 'r') as z:
        # 1. 找 OPF 路徑
        container = z.read('META-INF/container.xml').decode('utf-8', errors='ignore')
        m = re.search(r'full-path="([^"]+\.opf)"', container)
        if not m:
            raise RuntimeError("找不到 OPF 檔案")
        opf_path = m.group(1)
        opf_dir = '/'.join(opf_path.split('/')[:-1])
        opf = z.read(opf_path).decode('utf-8', errors='ignore')

        # 2. manifest id → href
        manifest = {}
        for item in re.finditer(
                r'<item\b[^>]+\bid="([^"]+)"[^>]+\bhref="([^"]+)"', opf):
            manifest[item.group(1)] = item.group(2)

        # 3. spine 順序
        spine_ids = re.findall(r'<itemref\b[^>]+\bidref="([^"]+)"', opf)

        # 4. 逐章擷取
        chapters = []
        total = 0
        for sid in spine_ids:
            href = manifest.get(sid)
            if not href:
                continue
            fp = f"{opf_dir}/{href}" if opf_dir else href
            try:
                html = z.read(fp).decode('utf-8', errors='ignore')
            except KeyError:
                continue
            parser = _TextExtractor()
            parser.feed(html)
            text = re.sub(r'\n{3,}', '\n\n', parser.get_text()).strip()
            if text:
                chapters.append(text)
                total += len(text)
                if total >= max_chars:
                    break

    full = '\n\n---\n\n'.join(chapters)
    return full[:max_chars]


# ── Claude AI 整理 ────────────────────────────────────────────────────────────

def summarize_with_claude(epub_text, book_name, api_key=None):
    """
    將 epub 文字送給 Claude Opus，以串流方式輸出整理重點，
    並儲存為 Markdown 檔案。
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    char_count = len(epub_text)
    print(f"   書本文字: {char_count:,} 字元，傳送至 Claude 分析中...\n")

    prompt = f"""以下是《{book_name}》的完整內容，請用**繁體中文**為我整理：

1. **核心主題** — 這本書在講什麼（2-3 句話）
2. **作者核心觀點** — 作者最重要的論述（條列）
3. **章節重點摘要** — 依章節順序，每章 2-5 個要點
4. **關鍵概念與詞彙** — 書中重要概念的簡要解釋
5. **最值得思考的洞見** — 你認為最深刻或最具啟發性的段落或觀點
6. **延伸閱讀建議** — 若想深入了解，可以閱讀哪些相關主題

---
書本內容：

{epub_text}"""

    summary_parts = []

    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system="你是一位優秀的書籍分析師，熟悉中文非虛構類寫作。請用清晰的繁體中文 Markdown 格式回應。",
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        for chunk in stream.text_stream:
            print(chunk, end="", flush=True)
            summary_parts.append(chunk)

    print("\n")
    return ''.join(summary_parts)


# ── 主程式 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="3ook.com NFT Book Downloader + AI 重點整理")
    parser.add_argument("url", help="OpenSea NFT URL")
    parser.add_argument("--token-id", type=int, default=None,
                        help="指定你持有的 Token ID（不指定時自動查詢）")
    parser.add_argument("--index", type=int, default=0,
                        help="書本檔案索引，預設 0")
    parser.add_argument("--output", default=None, help="輸出 epub 路徑")
    parser.add_argument("--summarize", action="store_true",
                        help="下載後用 Claude 整理重點（需要 ANTHROPIC_API_KEY）")
    parser.add_argument("--summary-only", action="store_true",
                        help="只整理重點，跳過下載（epub 須已存在）")
    args = parser.parse_args()

    private_key = os.environ.get("PRIVATE_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    if not private_key and not args.summary_only:
        print("錯誤: 請設定 PRIVATE_KEY 環境變數")
        print("範例: PRIVATE_KEY=0x私鑰 python3 read_nft_book.py <url>")
        sys.exit(1)

    if (args.summarize or args.summary_only) and not anthropic_key:
        print("錯誤: 使用 --summarize 需要設定 ANTHROPIC_API_KEY 環境變數")
        sys.exit(1)

    # 解析 OpenSea URL
    chain, contract, url_token_id = parse_opensea_url(args.url)
    rpc_url = CHAIN_RPC.get(chain)
    if not rpc_url:
        print(f"錯誤: 不支援的鏈 '{chain}'，支援: {', '.join(CHAIN_RPC)}")
        sys.exit(1)

    print(f"鏈:    {chain}")
    print(f"合約:  {contract}")
    print(f"URL Token ID: {url_token_id}")
    print()

    # Step 1: 從 tokenURI 取得書本 class_id
    print("1. 讀取 tokenURI 中繼資料...")
    try:
        uri = get_token_uri(rpc_url, contract, url_token_id)
        metadata = decode_token_metadata(uri)
        class_id = find_class_id_from_metadata(metadata, contract)
        book_name = metadata.get("name", "")
        print(f"   書名: {book_name}")
        print(f"   Class ID: {class_id}")
    except Exception as e:
        print(f"   警告: 無法讀取 tokenURI ({e})，直接使用合約地址")
        class_id = contract.lower()
        book_name = ""

    # Step 2: 授權
    print("2. 簽署授權訊息...")
    wallet, jwt = authorize(private_key)
    print(f"   錢包: {wallet}")
    print(f"   JWT 取得成功")

    # Step 3: 確定 nft_id
    if args.token_id is not None:
        nft_id = args.token_id
        print(f"3. 使用指定 Token ID: {nft_id}")
    else:
        print("3. 查詢持有的 Token ID...")
        nft_id = find_owned_token(rpc_url, contract, wallet, hint_token_id=url_token_id)
        if nft_id is None:
            print(f"   此錢包未持有此合約的 NFT")
            print(f"   提示：若確定持有，可用 --token-id <token_id> 強制指定")
            sys.exit(1)
        print(f"   找到 Token ID: {nft_id}")

    # 決定 epub 檔名
    if args.output is None and book_name:
        safe_name = re.sub(r'[\\/*?:"<>|]', "_", book_name).strip()
        epub_path = f"{safe_name}.epub"
    else:
        epub_path = args.output or f"nft_book_{class_id[-8:]}_{nft_id if 'nft_id' in dir() else 0}.epub"

    # Step 4: 下載（summary-only 時略過）
    if not args.summary_only:
        print(f"4. 下載 epub (index={args.index})...")
        epub_path = download_epub(class_id, nft_id, jwt,
                                  index=args.index, output_path=epub_path)
        print(f"\n完成！儲存至: {epub_path}")
    else:
        if not os.path.exists(epub_path):
            print(f"錯誤: 找不到 epub 檔案: {epub_path}")
            print("請先下載或用 --output 指定路徑")
            sys.exit(1)
        print(f"使用已存在的 epub: {epub_path}")

    # Step 5: AI 整理重點
    if args.summarize or args.summary_only:
        print(f"\n5. Claude 整理重點...")
        epub_text = extract_epub_text(epub_path)
        summary = summarize_with_claude(epub_text, book_name or epub_path, api_key=anthropic_key)

        # 儲存摘要
        summary_path = re.sub(r'\.epub$', '_重點整理.md', epub_path)
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(f"# 《{book_name}》重點整理\n\n")
            f.write(summary)
        print(f"摘要已儲存至: {summary_path}")


if __name__ == "__main__":
    main()
