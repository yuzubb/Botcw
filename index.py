import os
import time
import requests
import re
from flask import Flask, request, Response
from supabase import create_client, Client

app = Flask(__name__)

# --- 環境変数から設定を読み込む ---
CW_TOKEN = os.environ.get("CW_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Supabaseクライアントの初期化
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 起動時に一度だけBot自身のIDを取得
def get_bot_id():
    headers = {"X-ChatWorkToken": CW_TOKEN}
    try:
        res = requests.get("https://api.chatwork.com/v2/me", headers=headers).json()
        return str(res.get("account_id", ""))
    except Exception as e:
        print(f"Error fetching BOT_ID: {e}")
        return ""

BOT_ID = get_bot_id()

# --- データベース操作関数 ---
def get_user(acc_id):
    # Supabaseからユーザー取得、いなければ作成
    res = supabase.table("profiles").select("*").eq("id", acc_id).execute()
    if not res.data:
        new_user = {
            "id": acc_id, 
            "points": 0, 
            "is_seller": False, 
            "admin_bonus_given": False, 
            "is_blacklisted": False
        }
        supabase.table("profiles").insert(new_user).execute()
        return new_user
    return res.data[0]

def update_user(acc_id, data):
    supabase.table("profiles").update(data).eq("id", acc_id).execute()

def send_cw(room_id, account_id, message_id, text):
    headers = {"X-ChatWorkToken": CW_TOKEN}
    body = f"[rp aid={account_id} to={room_id}-{message_id}][pname:{account_id}]さん\n{text}"
    requests.post(f"https://api.chatwork.com/v2/rooms/{room_id}/messages", headers=headers, data={"body": body})

def is_room_admin(room_id, account_id):
    headers = {"X-ChatWorkToken": CW_TOKEN}
    try:
        members = requests.get(f"https://api.chatwork.com/v2/rooms/{room_id}/members", headers=headers).json()
        for m in members:
            if str(m.get("account_id")) == str(account_id):
                return m.get("role") == "admin"
    except: return False
    return False

@app.route("/", methods=["GET"])
def index():
    return "Bot is running with Environment Variables!"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data: return Response(status=200)
    
    event = data.get("webhook_event", {})
    acc_id = str(event.get("account_id", ""))
    
    # Bot自身の発言は無視
    if not acc_id or acc_id == BOT_ID: 
        return Response(status=200)

    room_id = event.get("room_id")
    msg_id = event.get("message_id")
    body = event.get("body", "").strip()

    # ユーザーデータ取得
    user = get_user(acc_id)
    if user.get("is_blacklisted"): return Response(status=200)
    
    admin_auth = is_room_admin(room_id, acc_id)

    # --- 管理者ボーナス判定 ---
    if admin_auth and not user.get("admin_bonus_given"):
        new_points = (user.get("points") or 0) + 100000
        update_user(acc_id, {"points": new_points, "admin_bonus_given": True})
        send_cw(room_id, acc_id, msg_id, "✨管理者ボーナス 100,000pt 付与完了！")
        user["points"] = new_points

    # --- コマンド処理 ---
    if body == "/shop":
        res = supabase.table("items").select("*").execute()
        items = res.data
        if not items:
            send_cw(room_id, acc_id, msg_id, "現在ショップに商品はありません。")
        else:
            msg = "[info][title]🏪 ショップ[/title]"
            for i in items:
                msg += f"ID: {i['id']} | {i['name']} ({i['price']}pt)\n┗ {i['description']}\n"
            msg += "------------------\n購入: [code]/buy ID[/code][/info]"
            send_cw(room_id, acc_id, msg_id, msg)

    elif body.startswith("/buy "):
        item_id = body.replace("/buy ", "").strip()
        res = supabase.table("items").select("*").eq("id", item_id).execute()
        if res.data:
            item = res.data[0]
            current_points = user.get("points") or 0
            if current_points >= item["price"]:
                update_user(acc_id, {"points": current_points - item["price"]})
                send_cw(room_id, acc_id, msg_id, f"購入完了！\n[hr]{item['name']}\nURL: {item['url']}")
            else:
                send_cw(room_id, acc_id, msg_id, "ptが足りません。")
        else:
            send_cw(room_id, acc_id, msg_id, "指定されたIDの商品が見つかりません。")

    elif body == "/status":
        role = "✅販売人" if user.get("is_seller") else "❌一般"
        send_cw(room_id, acc_id, msg_id, f"所持: {user.get('points') or 0} pt\n権限: {role}")

    elif body.startswith("/give"):
        parts = re.findall(r'\d+', body)
        if len(parts) >= 2:
            target_id, amount = parts[0], int(parts[1])
            current_points = user.get("points") or 0
            if amount > 0 and current_points >= amount and target_id != acc_id:
                target_user = get_user(target_id)
                update_user(acc_id, {"points": current_points - amount})
                update_user(target_id, {"points": (target_user.get("points") or 0) + amount})
                send_cw(room_id, acc_id, msg_id, f"[pname:{target_id}]さんに {amount}pt 送金しました！")

    elif body == "/unlock":
        current_points = user.get("points") or 0
        if not user.get("is_seller") and current_points >= 5000:
            update_user(acc_id, {"points": current_points - 5000, "is_seller": True})
            send_cw(room_id, acc_id, msg_id, "🎉販売人権限を解放しました！")

    elif admin_auth and body.startswith("/additem "):
        # 形式: /additem ID,名前,価格,説明,URL
        raw = body.replace("/additem ", "").strip().split(",")
        if len(raw) >= 5:
            item_data = {
                "id": raw[0], 
                "name": raw[1], 
                "price": int(raw[2]), 
                "description": raw[3], 
                "url": raw[4]
            }
            supabase.table("items").upsert(item_data).execute()
            send_cw(room_id, acc_id, msg_id, f"✅ 商品「{raw[1]}」を登録しました。")

    # --- 1pt加算 (通常発言) ---
    update_user(acc_id, {"points": (user.get("points") or 0) + 1})
    
    return Response(status=200)
