import os
import random
import logging
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, request, Response
from supabase import create_client, Client

# --- 初期設定 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 日本標準時 (JST) の設定
JST = timezone(timedelta(hours=+9))

# 環境変数（RenderやHerokuの環境設定で入力してください）
CW_TOKEN = os.environ.get("CW_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not all([CW_TOKEN, SUPABASE_URL, SUPABASE_KEY]):
    logger.error("環境変数が不足しています。")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- 共通関数 ---

def get_bot_id():
    """Bot自身のChatWorkアカウントIDを取得"""
    try:
        headers = {"X-ChatWorkToken": CW_TOKEN}
        res = requests.get("https://api.chatwork.com/v2/me", headers=headers).json()
        return str(res.get("account_id", ""))
    except Exception as e:
        logger.error(f"Bot ID取得失敗: {e}")
        return ""

BOT_ID = get_bot_id()

def get_user(acc_id):
    """ユーザー情報を取得。存在しない場合は新規作成"""
    res = supabase.table("profiles").select("*").eq("id", acc_id).execute()
    if not res.data:
        new_user = {
            "id": acc_id,
            "points": 0,
            "is_seller": False,
            "job": "なし",
            "is_blacklisted": False,
            "work_count": 0,
            "last_work_at": None,
            "last_work_day": None,
            "last_omikuji_at": None,
            "last_steal_at": None,
            "last_steal_day": None,
            "steal_count": 0
        }
        supabase.table("profiles").insert(new_user).execute()
        return new_user
    return res.data[0]

def update_user(acc_id, data):
    """ユーザー情報の更新"""
    supabase.table("profiles").update(data).eq("id", acc_id).execute()

def send_cw(room_id, account_id, message_id, text):
    """ChatWorkに返信（Reply形式）を送信"""
    headers = {"X-ChatWorkToken": CW_TOKEN}
    body = f"[rp aid={account_id} to={room_id}-{message_id}][pname:{account_id}]さん\n{text}"
    requests.post(
        f"https://api.chatwork.com/v2/rooms/{room_id}/messages", 
        headers=headers, 
        data={"body": body}
    )

def is_room_admin(room_id, account_id):
    """そのルームで管理権限を持っているか確認"""
    headers = {"X-ChatWorkToken": CW_TOKEN}
    try:
        members = requests.get(f"https://api.chatwork.com/v2/rooms/{room_id}/members", headers=headers).json()
        return any(str(m.get("account_id")) == str(account_id) and m.get("role") == "admin" for m in members)
    except:
        return False

# --- ルーティング ---

@app.route("/", methods=["GET"])
def index():
    return "Bot Active"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data:
        return Response(status=200)

    event = data.get("webhook_event", {})
    acc_id = str(event.get("account_id", ""))
    room_id = event.get("room_id")
    msg_id = event.get("message_id")
    body = event.get("body", "").strip()

    # Bot自身の発言は無視
    if not acc_id or acc_id == BOT_ID:
        return Response(status=200)

    user = get_user(acc_id)
    if user.get("is_blacklisted"):
        return Response(status=200)

    is_admin = is_room_admin(room_id, acc_id)
    now = datetime.now(JST)
    today = now.date().isoformat()

    # --- コマンド処理 ---

    # 1. おみくじ
    if body == "/omikuji":
        if user.get("last_omikuji_at") == today:
            send_cw(room_id, acc_id, msg_id, "おみくじは1日1回です。")
        else:
            res = random.choice([
                {"n": "大吉", "p": 100},
                {"n": "吉", "p": 50},
                {"n": "凶", "p": 5}
            ])
            update_user(acc_id, {
                "points": (user.get("points") or 0) + res["p"], 
                "last_omikuji_at": today
            })
            send_cw(room_id, acc_id, msg_id, f"結果：{res['n']} ({res['p']}pt獲得)")

    # 2. ステータス確認
    elif body.startswith("/status"):
        parts = body.split(" ")
        target_id = parts[1] if len(parts) >= 2 else acc_id
        target = get_user(target_id)
        msg = (f"[pname:{target_id}]さんのステータス\n"
               f"所持: {target.get('points')}pt\n"
               f"職業: {target.get('job')}\n"
               f"販売権: {'あり' if target.get('is_seller') else 'なし'}")
        send_cw(room_id, acc_id, msg_id, msg)

    # 3. ショップ
    elif body == "/shop":
        items = supabase.table("items").select("*").execute().data
        msg = "[info][title]🏪 ショップ[/title]" + \
              "".join([f"ID:{i['id']} {i['name']}({i['price']}pt)\n" for i in items]) + \
              "購入: /buy ID[/info]"
        send_cw(room_id, acc_id, msg_id, msg)

    elif body.startswith("/buy "):
        item_id = body.split(" ")[1]
        item_data = supabase.table("items").select("*").eq("id", item_id).execute().data
        if item_data and (user.get('points') or 0) >= item_data[0]['price']:
            item = item_data[0]
            update_user(acc_id, {"points": user['points'] - item['price']})
            send_cw(room_id, acc_id, msg_id, f"購入完了: {item['name']}\nURL: {item['url']}")
        else:
            send_cw(room_id, acc_id, msg_id, "pt不足または商品が見つかりません。")

    # 4. 職業・仕事
    elif body == "/job":
        jobs = supabase.table("jobs").select("*").execute().data
        msg = "[info][title]💼 職業一覧[/title]" + \
              "".join([f"・{j['name']} (費用:{j['price']}pt)\n" for j in jobs]) + \
              "就職: /job 職業名[/info]"
        send_cw(room_id, acc_id, msg_id, msg)

    elif body.startswith("/job "):
        name = body.replace("/job ", "").strip()
        job_data = supabase.table("jobs").select("*").eq("name", name).execute().data
        if job_data and (user.get('points') or 0) >= job_data[0]['price']:
            update_user(acc_id, {"points": user['points'] - job_data[0]['price'], "job": name})
            send_cw(room_id, acc_id, msg_id, f"{name}に就職しました！")
        else:
            send_cw(room_id, acc_id, msg_id, "条件を満たしていないか、職業が存在しません。")

    elif body == "/work":
        job_info = supabase.table("jobs").select("*").eq("name", user.get("job")).execute().data
        if not job_info or user.get("job") == "なし":
            send_cw(room_id, acc_id, msg_id, "/job で就職してから働いてください。")
        else:
            can_work = True
            # 30分(1800秒)のクールタイム判定
            if user.get('last_work_at'):
                last_work = datetime.fromisoformat(user['last_work_at'].replace('Z', '+00:00'))
                diff = (now - last_work).total_seconds()
                if diff < 1800:
                    wait_min = int((1800 - diff) // 60)
                    send_cw(room_id, acc_id, msg_id, f"休憩中です（あと {wait_min} 分）")
                    can_work = False

            if can_work:
                count = user.get('work_count', 0) if user.get('last_work_day') == today else 0
                if count >= 10:
                    send_cw(room_id, acc_id, msg_id, "本日の労働上限（10回）に達しました。")
                else:
                    reward = random.randint(job_info[0]['min_pt'], job_info[0]['max_pt'])
                    if random.random() < 0.05: # 5%でボーナス
                        reward *= 2
                        text = f"🌟ボーナス発生！ {reward}pt 獲得しました！"
                    else:
                        text = f"{reward}pt 獲得しました。"
                    
                    update_user(acc_id, {
                        "points": (user.get("points") or 0) + reward,
                        "last_work_at": now.isoformat(),
                        "last_work_day": today,
                        "work_count": count + 1
                    })
                    send_cw(room_id, acc_id, msg_id, text)

    # 5. 特殊アクション
    elif body.startswith("/give "):
        parts = body.split(" ")
        if len(parts) >= 3:
            target_id, amt_str = parts[1], parts[2]
            if amt_str.isdigit() and int(amt_str) > 0:
                amt = int(amt_str)
                if (user.get("points") or 0) >= amt and target_id != acc_id:
                    target_user = get_user(target_id)
                    update_user(acc_id, {"points": user["points"] - amt})
                    update_user(target_id, {"points": (target_user.get("points") or 0) + amt})
                    send_cw(room_id, acc_id, msg_id, f"[pname:{target_id}]さんに{amt}pt送金しました。")
                else:
                    send_cw(room_id, acc_id, msg_id, "送金できません（pt不足など）")

    elif body == "/steal":
        if user.get("job") != "泥棒":
            send_cw(room_id, acc_id, msg_id, "泥棒に就職している必要があります。")
        else:
            # 10分(600秒)のクールタイム判定
            if user.get("last_steal_at"):
                last_steal = datetime.fromisoformat(user["last_steal_at"].replace("Z", "+00:00"))
                diff = (now - last_steal).total_seconds()
                if diff < 600:
                    wait_min = int((600 - diff) // 60)
                    wait_sec = int((600 - diff) % 60)
                    send_cw(room_id, acc_id, msg_id, f"クールタイム中です（あと {wait_min} 分 {wait_sec} 秒）")
                    return Response(status=200)

            # 日付が変わっていたらカウントリセット
            steal_count = user.get("steal_count", 0) if user.get("last_steal_day") == today else 0
            if steal_count >= 10:
                send_cw(room_id, acc_id, msg_id, "盗みは1日10回までです。")
            else:
                # 回数・最終実行時刻を更新
                update_user(acc_id, {"last_steal_at": now.isoformat(), "last_steal_day": today, "steal_count": steal_count + 1})
                if random.random() >= 0.5:  # 成功率50%
                    send_cw(room_id, acc_id, msg_id, "💨 盗みに失敗…逃げ切りました！")
                else:
                    targets = supabase.table("profiles").select("*").neq("id", acc_id).gt("points", 0).execute().data
                    if targets:
                        t = random.choice(targets)
                        amt = random.randint(100, 500)
                        amt = min(amt, t['points'])  # 相手のptを超えないようにキャップ
                        update_user(acc_id, {"points": (user.get('points') or 0) + amt})
                        update_user(t['id'], {"points": t['points'] - amt})  # マイナスなし
                        send_cw(room_id, acc_id, msg_id, f"✅ 成功！[pname:{t['id']}]から{amt}pt奪いました！")
                    else:
                        send_cw(room_id, acc_id, msg_id, "盗める相手がいませんでした。")

    elif body == "/unlock":
        if user.get("is_seller"):
            send_cw(room_id, acc_id, msg_id, "既に販売人です。")
        elif (user.get("points") or 0) >= 5000:
            update_user(acc_id, {"points": user["points"] - 5000, "is_seller": True})
            send_cw(room_id, acc_id, msg_id, "5000pt支払い、販売権を獲得しました！")
        else:
            send_cw(room_id, acc_id, msg_id, "販売権の解放には5000pt必要です。")

    elif body.startswith("/sell "):
        if user.get("is_seller"):
            p = body.split(None, 2)
            if len(p) >= 3:
                msg = (f"[info][title]📢 出品のお知らせ[/title]"
                       f"品名: {p[1]}\n価格: {p[2]}pt\n"
                       f"購入希望者は [code]/give {acc_id} {p[2]}[/code] で送金してください[/info]")
                send_cw(room_id, acc_id, msg_id, msg)
        else:
            send_cw(room_id, acc_id, msg_id, "/unlock で販売権を得る必要があります。")

    # 6. 管理者コマンド
    elif is_admin:
        if body.startswith("/add_job "):
            d = body.replace("/add_job ", "").split(",")
            if len(d) == 4:
                supabase.table("jobs").upsert({"name":d[0], "price":int(d[1]), "min_pt":int(d[2]), "max_pt":int(d[3])}).execute()
                send_cw(room_id, acc_id, msg_id, "職業を追加しました。")
        elif body.startswith("/del_job "):
            name = body.replace("/del_job ", "").strip()
            supabase.table("jobs").delete().eq("name", name).execute()
            send_cw(room_id, acc_id, msg_id, "職業を削除しました。")
        elif body.startswith("/add_item "):
            d = body.replace("/add_item ", "").split(",")
            if len(d) == 5:
                supabase.table("items").upsert({"id":d[0], "name":d[1], "price":int(d[2]), "description":d[3], "url":d[4]}).execute()
                send_cw(room_id, acc_id, msg_id, "商品を追加しました。")

    # 最後に、通常発言に対して+1pt加算
    update_user(acc_id, {"points": (user.get("points") or 0) + 1})

    return Response(status=200)

if __name__ == "__main__":
    # ローカル実行用。デプロイ時は環境に合わせたポートが使われます
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
