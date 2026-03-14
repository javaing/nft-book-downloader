# NFT Book Downloader

透過 Base 錢包驗證 NFT 持有，從 [3ook.com](https://3ook.com) / [Liker Land](https://liker.land) 下載 epub 電子書。

## 功能

- 解析任意 OpenSea NFT URL，自動識別書本合約
- 支援多條鏈：Base、Ethereum、Polygon、Optimism、Arbitrum
- 用私鑰簽署 EIP-191 訊息，向 LikeCoin API 取得 JWT
- 自動查詢錢包持有的 Token ID（支援 ERC-721 Enumerable 及 `ownerOf` fallback）
- 以書名作為輸出檔名

## 安裝

```bash
pip install eth-account
```

## 用法

```bash
PRIVATE_KEY=0x你的私鑰 python3 read_nft_book.py <opensea_url>
```

### 範例

```bash
# 自動偵測持有的 Token ID
PRIVATE_KEY=0x... python3 read_nft_book.py \
  https://opensea.io/item/base/0x67018c5ff51e2c84badb13e15e4dad6d32d3498e/0

# 指定 Token ID
PRIVATE_KEY=0x... python3 read_nft_book.py \
  https://opensea.io/item/base/0x67018c5ff51e2c84badb13e15e4dad6d32d3498e/0 \
  --token-id 5

# 指定輸出路徑
PRIVATE_KEY=0x... python3 read_nft_book.py \
  https://opensea.io/item/base/0x67018c5ff51e2c84badb13e15e4dad6d32d3498e/0 \
  --output my_book.epub
```

### 選項

| 選項 | 說明 |
|------|------|
| `--token-id` | 指定你持有的 Token ID（省略時自動查詢） |
| `--index` | 書本檔案索引，預設 0（第一個 epub） |
| `--output` | 輸出 epub 路徑（省略時使用書名） |

## 支援的 OpenSea URL 格式

```
https://opensea.io/assets/<chain>/<contract>/<token_id>
https://opensea.io/item/<chain>/<contract>/<token_id>
```

## 注意事項

- **私鑰安全**：務必用環境變數傳入，不要直接寫在指令列或程式中
- 此工具僅支援 [3ook.com](https://3ook.com) / [Liker Land](https://liker.land) 發行的 NFT 書本

## 技術細節

1. 從合約呼叫 `tokenURI()` 取得書本 metadata
2. 用 `personal_sign`（EIP-191）向 `api.like.co/wallet/authorize` 取得 JWT
3. 透過 `api.like.co/ebook-cors/` 下載 epub

## License

MIT
