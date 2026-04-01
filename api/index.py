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
JST = timezone(timedelta(hours=+9))

CW_TOKEN = os.environ.get("CW_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not all([CW_TOKEN, SUPABASE_URL, SUPABASE_KEY]):
    logger.error("環境変数が不足しています。")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- 共通関数 ---

def get_bot_id():
    try:
        headers = {"X-ChatWorkToken": CW_TOKEN}
        res = requests.get("https://api.chatwork.com/v2/me", headers=headers).json()
        return str(res.get("account_id", ""))
    except Exception as e:
        logger.error(f"Bot ID取得失敗: {e}")
        return ""

BOT_ID = get_bot_id()

def get_user(acc_id):
    res = supabase.table("profiles").select("*").eq("id", acc_id).execute()
    if not res.data:
        new_user = {
            "id": acc_id, "points": 0, "is_seller": False, "job": "なし",
            "is_blacklisted": False, "work_count": 0, "last_work_at": None,
            "last_work_day": None, "last_omikuji_at": None, "last_steal_at": None,
            "last_steal_day": None, "steal_count": 0
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
        return any(str(m.get("account_id")) == str(account_id) and m.get("role") == "admin" for m in members)
    except:
        return False

# --- ルーティング ---

@app.route("/", methods=["GET"])
def index():
    return "Bot Active"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        if not data: return Response(status=200)

        event = data.get("webhook_event", {})
        acc_id = str(event.get("account_id", ""))
        room_id = event.get("room_id")
        msg_id = event.get("message_id")
        body = event.get("body", "").strip()

        if not acc_id or acc_id == BOT_ID: return Response(status=200)

        user = get_user(acc_id)
        if user.get("is_blacklisted"): return Response(status=200)

        is_admin = is_room_admin(room_id, acc_id)
        now = datetime.now(JST)
        today = now.date().isoformat()
        u_pts = user.get("points") or 0

        # 1. おみくじ
        if body == "/omikuji":
            if user.get("last_omikuji_at") == today:
                send_cw(room_id, acc_id, msg_id, "おみくじは1日1回です。")
            else:
                res = random.choice([{"n": "大吉", "p": 100}, {"n": "吉", "p": 50}, {"n": "凶", "p": 5}])
                update_user(acc_id, {"points": u_pts + res["p"], "last_omikuji_at": today})
                send_cw(room_id, acc_id, msg_id, f"結果：{res['n']} ({res['p']}pt獲得)")

        # 2. ステータス
        elif body.startswith("/status"):
            parts = body.split(" ")
            target_id = parts[1] if len(parts) >= 2 else acc_id
            t = get_user(target_id)
            msg = (f"[pname:{target_id}]さんのステータス\n所持: {t.get('points', 0)}pt\n"
                   f"職業: {t.get('job', 'なし')}\n販売権: {'あり' if t.get('is_seller') else 'なし'}")
            send_cw(room_id, acc_id, msg_id, msg)

        # 3. ショップ & 購入
        elif body == "/shop":
            items = supabase.table("items").select("*").execute().data or []
            msg = "[info][title]🏪 ショップ[/title]" + \
                  "".join([f"ID:{i['id']} {i['name']}({i['price']}pt)\n" for i in items]) + \
                  "購入: /buy ID[/info]"
            send_cw(room_id, acc_id, msg_id, msg)

        elif body.startswith("/buy "):
            item_id = body.split(" ")[1] if len(body.split(" ")) > 1 else ""
            item_data = supabase.table("items").select("*").eq("id", item_id).execute().data
            if item_data and u_pts >= item_data[0]['price']:
                item = item_data[0]
                update_user(acc_id, {"points": u_pts - item['price']})
                send_cw(room_id, acc_id, msg_id, f"購入完了: {item['name']}\nURL: {item['url']}")
            else:
                send_cw(room_id, acc_id, msg_id, "pt不足または商品が見つかりません。")

        # 4. 職業 & 仕事
        elif body == "/job":
            jobs = supabase.table("jobs").select("*").execute().data or []
            msg = "[info][title]💼 職業一覧[/title]" + \
                  "".join([f"・{j['name']} (費用:{j['price']}pt)\n" for j in jobs]) + \
                  "就職: /job 職業名[/info]"
            send_cw(room_id, acc_id, msg_id, msg)

        elif body.startswith("/job "):
            name = body.replace("/job ", "").strip()
            job_data = supabase.table("jobs").select("*").eq("name", name).execute().data
            if job_data and u_pts >= job_data[0]['price']:
                update_user(acc_id, {"points": u_pts - job_data[0]['price'], "job": name})
                send_cw(room_id, acc_id, msg_id, f"{name}に就職しました！")
            else:
                send_cw(room_id, acc_id, msg_id, "条件を満たしていないか、職業が存在しません。")

        elif body == "/work":
            job_info = supabase.table("jobs").select("*").eq("name", user.get("job")).execute().data
            if not job_info or user.get("job") == "なし":
                send_cw(room_id, acc_id, msg_id, "/job で就職してから働いてください。")
            else:
                wait = 1800
                last_work_str = user.get('last_work_at')
                diff = (now - datetime.fromisoformat(last_work_str.replace('Z', '+00:00'))).total_seconds() if last_work_str else wait
                
                if diff < wait:
                    send_cw(room_id, acc_id, msg_id, f"休憩中（あと {int((wait-diff)//60)} 分）")
                else:
                    count = user.get('work_count', 0) if user.get('last_work_day') == today else 0
                    if count >= 10:
                        send_cw(room_id, acc_id, msg_id, "本日の労働上限です。")
                    else:
                        reward = random.randint(job_info[0]['min_pt'], job_info[0]['max_pt'])
                        if random.random() < 0.05: reward *= 2
                        update_user(acc_id, {"points": u_pts + reward, "last_work_at": now.isoformat(), "last_work_day": today, "work_count": count + 1})
                        send_cw(room_id, acc_id, msg_id, f"{reward}pt 獲得しました。")

        # 5. 送金 & 盗み
        elif body.startswith("/give "):
            p = body.split(" ")
            if len(p) >= 3 and p[2].isdigit():
                target_id, amt = p[1], int(p[2])
                if u_pts >= amt and target_id != acc_id:
                    t_user = get_user(target_id)
                    update_user(acc_id, {"points": u_pts - amt})
                    update_user(target_id, {"points": (t_user.get("points") or 0) + amt})
                    send_cw(room_id, acc_id, msg_id, f"[pname:{target_id}]さんに{amt}pt送金しました。")

        elif body == "/steal":
            if user.get("job") != "泥棒":
                send_cw(room_id, acc_id, msg_id, "泥棒に就職してください。")
            else:
                last_s = user.get("last_steal_at")
                s_diff = (now - datetime.fromisoformat(last_s.replace("Z", "+00:00"))).total_seconds() if last_s else 600
                if s_diff < 600:
                    send_cw(room_id, acc_id, msg_id, "クールタイム中。")
                else:
                    s_count = user.get("steal_count", 0) if user.get("last_steal_day") == today else 0
                    if s_count >= 10:
                        send_cw(room_id, acc_id, msg_id, "1日10回までです。")
                    else:
                        update_user(acc_id, {"last_steal_at": now.isoformat(), "last_steal_day": today, "steal_count": s_count + 1})
                        if random.random() < 0.5:
                            send_cw(room_id, acc_id, msg_id, "💨 失敗…！")
                        else:
                            targets = supabase.table("profiles").select("*").neq("id", acc_id).gt("points", 0).execute().data
                            if targets:
                                t = random.choice(targets)
                                amt = min(random.randint(100, 500), t['points'])
                                update_user(acc_id, {"points": u_pts + amt}); update_user(t['id'], {"points": t['points'] - amt})
                                send_cw(room_id, acc_id, msg_id, f"✅ 成功！[pname:{t['id']}]から{amt}pt奪いました！")

        # 6. 販売権
        elif body == "/unlock":
            if user.get("is_seller"): send_cw(room_id, acc_id, msg_id, "取得済みです。")
            elif u_pts >= 5000:
                update_user(acc_id, {"points": u_pts - 5000, "is_seller": True})
                send_cw(room_id, acc_id, msg_id, "販売権を獲得！")
            else: send_cw(room_id, acc_id, msg_id, "5000pt必要です。")

        elif body.startswith("/sell "):
            if user.get("is_seller"):
                p = body.split(None, 2)
                if len(p) >= 3:
                    send_cw(room_id, acc_id, msg_id, f"[info][title]📢 出品[/title]品名: {p[1]}\n価格: {p[2]}pt\n/give {acc_id} {p[2]} で購入可[/info]")
            else: send_cw(room_id, acc_id, msg_id, "販売権が必要です。")

        # 7. 管理者（仕事・アイテム追加）
        elif is_admin:
            if body.startswith("/add_job "):
                d = body.replace("/add_job ", "").split(",")
                if len(d) == 4:
                    supabase.table("jobs").upsert({"name":d[0], "price":int(d[1]), "min_pt":int(d[2]), "max_pt":int(d[3])}).execute()
                    send_cw(room_id, acc_id, msg_id, "職業を追加しました。")
            elif body.startswith("/add_item "):
                d = body.replace("/add_item ", "").split(",")
                if len(d) == 5:
                    supabase.table("items").upsert({"id":d[0], "name":d[1], "price":int(d[2]), "description":d[3], "url":d[4]}).execute()
                    send_cw(room_id, acc_id, msg_id, "商品を追加しました。")

        # 通常発言ポイント加算
        update_user(acc_id, {"points": u_pts + 1})

    except Exception as e:
        logger.error(f"Error: {e}")
    return Response(status=200)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
