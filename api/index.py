import os
import random
import requests
from datetime import datetime
from flask import Flask, request, Response
from supabase import create_client, Client

app = Flask(__name__)

# 環境変数
CW_TOKEN = os.environ.get("CW_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_bot_id():
    try:
        headers = {"X-ChatWorkToken": CW_TOKEN}
        res = requests.get("https://api.chatwork.com/v2/me", headers=headers).json()
        return str(res.get("account_id", ""))
    except: return ""

BOT_ID = get_bot_id()

def get_user(acc_id):
    res = supabase.table("profiles").select("*").eq("id", acc_id).execute()
    if not res.data:
        new_user = {"id": acc_id, "points": 0, "is_seller": False, "job": "なし", "is_blacklisted": False}
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
    except: return False

@app.route("/", methods=["GET"])
def index(): return "Bot Active"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data: return Response(status=200)
    event = data.get("webhook_event", {})
    acc_id, room_id, msg_id, body = str(event.get("account_id", "")), event.get("room_id"), event.get("message_id"), event.get("body", "").strip()
    
    if not acc_id or acc_id == BOT_ID: return Response(status=200)
    user = get_user(acc_id)
    if user.get("is_blacklisted"): return Response(status=200)
    is_admin = is_room_admin(room_id, acc_id)

    # --- コマンド処理 (実行したら return する) ---

    if body == "/omikuji":
        today = datetime.now().date().isoformat()
        if user.get("last_omikuji_at") == today:
            send_cw(room_id, acc_id, msg_id, "おみくじは1日1回です。")
        else:
            res = random.choice([{"n":"大吉","p":100},{"n":"吉","p":50},{"n":"凶","p":5}])
            update_user(acc_id, {"points": (user.get("points") or 0) + res["p"], "last_omikuji_at": today})
            send_cw(room_id, acc_id, msg_id, f"結果：{res['n']} ({res['p']}pt獲得)")
        return Response(status=200)

    elif body == "/status":
        send_cw(room_id, acc_id, msg_id, f"所持: {user.get('points')}pt\n職業: {user.get('job')}\n販売人: {user.get('is_seller')}")
        return Response(status=200)

    elif body == "/shop":
        items = supabase.table("items").select("*").execute().data
        msg = "[info][title]🏪 ショップ[/title]" + "".join([f"ID:{i['id']} {i['name']}({i['price']}pt)\n" for i in items]) + "購入: /buy ID[/info]"
        send_cw(room_id, acc_id, msg_id, msg)
        return Response(status=200)

    elif body.startswith("/buy "):
        item_id = body.split(" ")[1]
        item = supabase.table("items").select("*").eq("id", item_id).execute().data
        if item and user['points'] >= item[0]['price']:
            update_user(acc_id, {"points": user['points'] - item[0]['price']})
            send_cw(room_id, acc_id, msg_id, f"購入完了: {item[0]['url']}")
        else: send_cw(room_id, acc_id, msg_id, "pt不足または商品なし")
        return Response(status=200)

    elif body == "/job":
        jobs = supabase.table("jobs").select("*").execute().data
        msg = "[info][title]💼 職業一覧[/title]" + "".join([f"・{j['name']} (費用:{j['price']}pt)\n" for j in jobs]) + "就職: /job 職業名[/info]"
        send_cw(room_id, acc_id, msg_id, msg)
        return Response(status=200)

    elif body.startswith("/job "):
        name = body.replace("/job ", "").strip()
        job = supabase.table("jobs").select("*").eq("name", name).execute().data
        if job and user['points'] >= job[0]['price']:
            update_user(acc_id, {"points": user['points'] - job[0]['price'], "job": name})
            send_cw(room_id, acc_id, msg_id, f"{name}に就職しました！")
        else: send_cw(room_id, acc_id, msg_id, "条件未達(pt不足など)")
        return Response(status=200)

    elif body == "/work":
        job = supabase.table("jobs").select("*").eq("name", user.get("job")).execute().data
        if not job: send_cw(room_id, acc_id, msg_id, "/job で就職してください。")
        else:
            now = datetime.now()
            today = now.date().isoformat()
            last_work = datetime.fromisoformat(user['last_work_at']) if user.get('last_work_at') else None
            count = user['work_count'] if user.get('last_work_day') == today else 0
            if last_work and (now - last_work).total_seconds() < 1800: send_cw(room_id, acc_id, msg_id, "休憩中(30分間)")
            elif count >= 10: send_cw(room_id, acc_id, msg_id, "1日10回まで")
            else:
                reward = random.randint(job[0]['min_pt'], job[0]['max_pt'])
                if random.random() < 0.05: reward *= 2 # ボーナス
                update_user(acc_id, {"points": user['points'] + reward, "last_work_at": now.isoformat(), "last_work_day": today, "work_count": count + 1})
                send_cw(room_id, acc_id, msg_id, f"{reward}pt 獲得！")
        return Response(status=200)

    elif body == "/steal":
        if user.get("job") != "泥棒": send_cw(room_id, acc_id, msg_id, "泥棒専用です。")
        else:
            today = datetime.now().date().isoformat()
            if user.get("last_steal_at") == today: send_cw(room_id, acc_id, msg_id, "1日1回まで")
            else:
                targets = supabase.table("profiles").select("*").neq("id", acc_id).gt("points", 0).execute().data
                if targets:
                    t = random.choice(targets)
                    amt = random.randint(1, 50)
                    update_user(acc_id, {"points": user['points'] + amt, "last_steal_at": today})
                    update_user(t['id'], {"points": t['points'] - amt})
                    send_cw(room_id, acc_id, msg_id, f"[pname:{t['id']}]から{amt}pt奪いました")
        return Response(status=200)

    elif body == "/unlock":
        if user.get("is_seller"): send_cw(room_id, acc_id, msg_id, "既に販売人です。")
        elif (user.get("points") or 0) >= 5000:
            update_user(acc_id, {"points": user["points"] - 5000, "is_seller": True})
            send_cw(room_id, acc_id, msg_id, "販売人になりました！")
        else: send_cw(room_id, acc_id, msg_id, "5000pt必要です。")
        return Response(status=200)

    elif body.startswith("/sell "):
        if user.get("is_seller"):
            p = body.split(None, 2)
            if len(p) >= 3: send_cw(room_id, acc_id, msg_id, f"[info][title]📢 出品[/title]品: {p[1]}\n価: {p[2]}pt\n購入は [code]/give {acc_id} {p[2]}[/code][/info]")
        else: send_cw(room_id, acc_id, msg_id, "/unlock が必要です。")
        return Response(status=200)

    # --- 管理者コマンド ---
    if is_admin:
        if body.startswith("/add_job "):
            d = body.replace("/add_job ", "").split(",")
            supabase.table("jobs").upsert({"name":d[0],"price":int(d[1]),"min_pt":int(d[2]),"max_pt":int(d[3])}).execute()
            send_cw(room_id, acc_id, msg_id, "仕事追加完了")
            return Response(status=200)
        elif body.startswith("/del_job "):
            supabase.table("jobs").delete().eq("name", body.replace("/del_job ", "").strip()).execute()
            send_cw(room_id, acc_id, msg_id, "仕事削除完了")
            return Response(status=200)
        elif body.startswith("/add_item "):
            d = body.replace("/add_item ", "").split(",")
            supabase.table("items").upsert({"id":d[0],"name":d[1],"price":int(d[2]),"description":d[3],"url":d[4]}).execute()
            send_cw(room_id, acc_id, msg_id, "商品追加完了")
            return Response(status=200)
        elif body.startswith("/del_item "):
            supabase.table("items").delete().eq("id", body.replace("/del_item ", "").strip()).execute()
            send_cw(room_id, acc_id, msg_id, "商品削除完了")
            return Response(status=200)

    # --- 通常発言 (コマンド以外の時だけ +1pt) ---
    update_user(acc_id, {"points": (user.get("points") or 0) + 1})
    return Response(status=200)

if __name__ == "__main__":
    app.run(port=5000)
