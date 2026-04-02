import os
import random
import requests
from datetime import datetime
from flask import Flask, request, Response
from supabase import create_client, Client

app = Flask(__name__)

CW_TOKEN = os.environ.get("CW_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

_CACHED_BOT_ID = None

def get_bot_id():
    global _CACHED_BOT_ID
    if _CACHED_BOT_ID:
        return _CACHED_BOT_ID
    try:
        headers = {"X-ChatWorkToken": CW_TOKEN}
        res = requests.get("https://api.chatwork.com/v2/me", headers=headers).json()
        _CACHED_BOT_ID = str(res.get("account_id", ""))
        return _CACHED_BOT_ID
    except:
        return ""

def get_user(acc_id):
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
            "last_work_day": None
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
    except: return False

def create_trade_room(item_name, item_url, buyer_id):
    headers = {"X-ChatWorkToken": CW_TOKEN}
    bot_id = get_bot_id()
    if not bot_id:
        return None, "Bot ID error"
    room_data = {
        "name": f"【取引】{item_name}",
        "description": f"商品: {item_name}\nURL: {item_url}\n購入者ID: {buyer_id}",
        "link": 1,
        "link_need_acceptance": 0,
        "members_admin_ids": bot_id  
    }
    try:
        r_res = requests.post("https://api.chatwork.com/v2/rooms", headers=headers, data=room_data).json()
        new_rid = r_res.get("room_id")
        if not new_rid:
            return None, str(r_res.get("errors") or r_res.get("message"))
        l_res = requests.get(f"https://api.chatwork.com/v2/rooms/{new_rid}/link", headers=headers).json()
        return l_res.get("public_url"), None
    except Exception as e:
        return None, str(e)

@app.route("/", methods=["GET"])
def index(): return "Bot Active"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data: return Response(status=200)
    event = data.get("webhook_event", {})
    acc_id = str(event.get("account_id", ""))
    room_id = event.get("room_id")
    msg_id = event.get("message_id")
    body = event.get("body", "").strip()

    if not acc_id or acc_id == get_bot_id(): return Response(status=200)

    user = get_user(acc_id)
    if user.get("is_blacklisted"): return Response(status=200)
    is_admin = is_room_admin(room_id, acc_id)

    if body == "/omikuji":
        today = datetime.now().date().isoformat()
        if user.get("last_omikuji_at") == today:
            send_cw(room_id, acc_id, msg_id, "おみくじは1日1回です。")
        else:
            res = random.choice([{"n":"大吉","p":100},{"n":"吉","p":50},{"n":"凶","p":5}])
            update_user(acc_id, {"points": (user.get("points") or 0) + res["p"], "last_omikuji_at": today})
            send_cw(room_id, acc_id, msg_id, f"結果：{res['n']} ({res['p']}pt獲得)")
        return Response(status=200)

    elif body.startswith("/status"):
        parts = body.split(" ")
        if len(parts) >= 2:
            target_id = parts[1]
            target = get_user(target_id)
            send_cw(room_id, acc_id, msg_id,
                f"[pname:{target_id}]さんのステータス\n"
                f"所持: {target.get('points')}pt\n"
                f"職業: {target.get('job')}\n"
                f"販売人: {target.get('is_seller')}"
            )
        else:
            send_cw(room_id, acc_id, msg_id,
                f"あなたのステータス\n"
                f"所持: {user.get('points')}pt\n"
                f"職業: {user.get('job')}\n"
                f"販売人: {user.get('is_seller')}"
            )
        return Response(status=200)

    elif body == "/shop":
        items = supabase.table("items").select("*").execute().data
        msg = "[info][title]🏪 ショップ[/title]" + "".join([f"ID:{i['id']} {i['name']}({i['price']}pt)\n" for i in items]) + "購入: /buy ID[/info]"
        send_cw(room_id, acc_id, msg_id, msg)
        return Response(status=200)

    elif body.startswith("/buy "):
        parts = body.split()
        if len(parts) >= 2:
            item_id = parts[1]
            item_res = supabase.table("items").select("*").eq("id", item_id).execute()
            if item_res.data:
                target = item_res.data[0]
                if (user.get("points") or 0) >= target["price"]:
                    link_url, errors = create_trade_room(target['name'], target.get('url', 'なし'), acc_id)
                    if link_url:
                        update_user(acc_id, {"points": user["points"] - target["price"]})
                        send_cw(room_id, acc_id, msg_id, f"購入完了！\n専用ルームはこちら:\n{link_url}")
                    else:
                        send_cw(room_id, acc_id, msg_id, f"ルーム作成に失敗したため購入を中断しました。\nエラー: {errors}")
                else:
                    send_cw(room_id, acc_id, msg_id, "ポイントが足りません。")
        return Response(status=200)
    
    elif body.startswith("/add_item "):
        if not user.get("is_seller"):
            send_cw(room_id, acc_id, msg_id, "エラー: 販売人権限がありません。")
        else:
            parts = body.split()
            if len(parts) >= 4:
                try:
                    item_name = parts[1]
                    price = int(parts[2])
                    item_url = " ".join(parts[3:])
                    supabase.table("items").insert({
                        "name": item_name,
                        "price": price,
                        "url": item_url,  
                        "seller_id": acc_id
                    }).execute()
                    send_cw(room_id, acc_id, msg_id, f"成功: 「{item_name}」を登録しました。")
                except ValueError:
                    send_cw(room_id, acc_id, msg_id, "エラー: 価格は数値で。")
            else:
                send_cw(room_id, acc_id, msg_id, "使用法: /add_item 名前 価格 URL")
        return Response(status=200)

    elif body == "/job":
        jobs = supabase.table("jobs").select("*").execute().data
        msg = "[info][title]💼 職業一覧[/title]" + "".join([f"・{j['name']} (費用:{j['price']}pt)\n" for j in jobs]) + "就職: /job 職業名[/info]"
        send_cw(room_id, acc_id, msg_id, msg)
        return Response(status=200)

    elif body.startswith("/job "):
        name = body.replace("/job ", "").strip()
        job = supabase.table("jobs").select("*").eq("name", name).execute().data
        if job and (user.get('points') or 0) >= job[0]['price']:
            update_user(acc_id, {"points": user['points'] - job[0]['price'], "job": name})
            send_cw(room_id, acc_id, msg_id, f"{name}に就職しました！")
        else: send_cw(room_id, acc_id, msg_id, "条件未達(pt不足など)")
        return Response(status=200)

    elif body == "/work":
        job_data = supabase.table("jobs").select("*").eq("name", user.get("job")).execute().data
        if not job_data or user.get("job") == "なし":
            send_cw(room_id, acc_id, msg_id, "/job で就職してください。")
        else:
            now = datetime.now()
            today = now.date().isoformat()
            can_work = True
            if user.get('last_work_at'):
                try:
                    last_work = datetime.fromisoformat(user['last_work_at'].replace('Z', '+00:00')).replace(tzinfo=None)
                    diff = (now - last_work).total_seconds()
                    if diff < 1800:
                        send_cw(room_id, acc_id, msg_id, f"休憩中(あと {int((1800-diff)//60)}分)")
                        can_work = False
                except: pass
            if can_work:
                count = user.get('work_count', 0) if user.get('last_work_day') == today else 0
                if count >= 10:
                    send_cw(room_id, acc_id, msg_id, "本日は10回働きました。また明日！")
                else:
                    reward = random.randint(job_data[0]['min_pt'], job_data[0]['max_pt'])
                    if random.random() < 0.05: reward *= 2
                    update_user(acc_id, {
                        "points": (user.get("points") or 0) + reward,
                        "last_work_at": now.isoformat(),
                        "last_work_day": today,
                        "work_count": count + 1
                    })
                    send_cw(room_id, acc_id, msg_id, f"{reward}pt 獲得！")
        return Response(status=200)

    elif is_admin and body == "/daily_reset":
        try:
            supabase.table("profiles").update({
                "work_count": 0, "last_work_day": None, "last_omikuji_at": None,
                "last_hack_at": None, "last_steal_at": None
            }).neq("id", "0").execute()
            send_cw(room_id, acc_id, msg_id, "デイリー制限をリセットしました。")
        except Exception as e:
            send_cw(room_id, acc_id, msg_id, f"エラー: {e}")
        return Response(status=200)

    elif body.startswith("/give "):
        parts = body.split(" ")
        if len(parts) >= 3:
            target_id, amt_str = parts[1], parts[2]
            if amt_str.isdigit():
                amt = int(amt_str)
                if amt > 0 and (user.get("points") or 0) >= amt and target_id != acc_id:
                    target_user = get_user(target_id)
                    update_user(acc_id, {"points": user["points"] - amt})
                    update_user(target_id, {"points": (target_user.get("points") or 0) + amt})
                    send_cw(room_id, acc_id, msg_id, f"[pname:{target_id}]さんに{amt}pt送りました")
                else: send_cw(room_id, acc_id, msg_id, "送金失敗")
        return Response(status=200)

    elif body.startswith("/hack "):
        if user.get("job") != "ハッカー":
            send_cw(room_id, acc_id, msg_id, "ハッカー専用です。")
        else:
            parts = body.split(" ")
            if len(parts) < 2:
                send_cw(room_id, acc_id, msg_id, "使い方: /hack ID")
            else:
                target_id = parts[1]
                today = datetime.now().date().isoformat()
                if target_id == acc_id:
                    send_cw(room_id, acc_id, msg_id, "自分は不可。")
                elif user.get("last_hack_at") == today:
                    send_cw(room_id, acc_id, msg_id, "1日1回まで。")
                else:
                    target_user = get_user(target_id)
                    update_user(acc_id, {"last_hack_at": today})
                    if random.random() < 0.4:
                        reward = random.randint(50, 1000)
                        update_user(target_id, {"points": max(0, (target_user.get("points") or 0) - 100)})
                        update_user(acc_id, {"points": (user.get("points") or 0) + reward})
                        send_cw(room_id, acc_id, msg_id, f"成功！{reward}pt獲得")
                    else:
                        send_cw(room_id, acc_id, msg_id, "失敗...")
        return Response(status=200)

    elif body == "/steal":
        if user.get("job") != "泥棒":
            send_cw(room_id, acc_id, msg_id, "泥棒専用です。")
        else:
            today = datetime.now().date().isoformat()
            if user.get("last_steal_at") == today:
                send_cw(room_id, acc_id, msg_id, "1日1回まで。")
            else:
                targets = supabase.table("profiles").select("*").neq("id", acc_id).gt("points", 0).execute().data
                if targets:
                    t = random.choice(targets)
                    amt = min(t['points'], random.randint(500, 1000))
                    update_user(acc_id, {"points": (user.get('points') or 0) + amt, "last_steal_at": today})
                    update_user(t['id'], {"points": t['points'] - amt})
                    send_cw(room_id, acc_id, msg_id, f"成功！[pname:{t['id']}]から{amt}pt奪取")
                else:
                    send_cw(room_id, acc_id, msg_id, "獲物がいません。")
        return Response(status=200)

    update_user(acc_id, {"points": (user.get("points") or 0) + 1})
    return Response(status=200)

if __name__ == "__main__":
    app.run()
