import os
import re
import random
import requests
from datetime import datetime, timedelta
from flask import Flask, request, Response
from supabase import create_client, Client

app = Flask(__name__)

CW_TOKEN = os.environ.get("CW_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

_CACHED_BOT_ID = None


# ─── 週間chat_count に応じた /work 上限 ──────────────────────
# chat_count は毎週月曜0:00にリセットされる週間投稿数
WORK_LIMIT_TIERS = [
    (750, None),   # 750投稿以上 → 無制限
    (500, 20),
    (300, 16),
    (150, 12),
    (50,  8),
    (0,   5),      # 0〜49投稿 → 5回/日
]

def get_work_limit(chat_count):
    for threshold, limit in WORK_LIMIT_TIERS:
        if chat_count >= threshold:
            return limit  # Noneは無制限


# ─── 日付が変わった瞬間に自動 daily_reset ────────────────────
def run_daily_reset():
    try:
        supabase.table("profiles").update({
            "work_count": 0,
            "last_work_day": None,
            "last_omikuji_at": None,
            "last_hack_at": None,
            "last_steal_at": None,
        }).neq("id", "0").execute()
        print(f"✅ [daily_reset] 完了: {datetime.now().isoformat()}")
        return True
    except Exception as e:
        print(f"❌ [daily_reset] エラー: {e}")
        return False


# ─── 宝くじ 週次抽選（毎週月曜0:00） ─────────────────────────
LOTTERY_TICKET_PRICE = 100
LOTTERY_WIN_RATE = 0.3      # 当選確率30%（外れでも積立は返ってこない）

def run_weekly_reset():
    """週間リセット（毎週月曜0:00）"""
    try:
        # 週間chat_countリセット
        supabase.table("profiles").update({"chat_count": 0}).neq("id", "0").execute()
        print(f"✅ [weekly_reset] chat_count リセット完了: {datetime.now().isoformat()}")
        return True
    except Exception as e:
        print(f"❌ [weekly_reset] エラー: {e}")
        return False

def run_lottery_draw():
    try:
        # 宝くじ抽選
        tickets = supabase.table("lottery_tickets").select("*").execute().data
        if not tickets:
            print("[lottery] 購入者なし、スキップ")
        else:
            jackpot = len(tickets) * LOTTERY_TICKET_PRICE
            winners = [t for t in tickets if random.random() < LOTTERY_WIN_RATE]

            if winners:
                prize = jackpot // len(winners)
                for w in winners:
                    u = supabase.table("profiles").select("points").eq("id", w["account_id"]).execute().data
                    if u:
                        supabase.table("profiles").update({
                            "points": (u[0].get("points") or 0) + prize
                        }).eq("id", w["account_id"]).execute()
                result_msg = f"🎰 宝くじ抽選結果！\n購入者: {len(tickets)}人 / 賞金総額: {jackpot}pt\n当選者: {len(winners)}人 / 1人あたり: {prize}pt"
            else:
                result_msg = f"🎰 宝くじ抽選結果！\n購入者: {len(tickets)}人 / 賞金総額: {jackpot}pt\n今週の当選者はいませんでした…賞金は没収されます。"

            supabase.table("lottery_tickets").delete().neq("account_id", "").execute()
            print(f"[lottery] {result_msg}")

        # 週間リセット実行
        run_weekly_reset()
        return True

    except Exception as e:
        print(f"❌ [lottery/weekly_reset] エラー: {e}")
        return False


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
            "last_work_day": None,
            "chat_count": 0,
        }
        supabase.table("profiles").insert(new_user).execute()
        return new_user
    return res.data[0]


def update_user(acc_id, data):
    supabase.table("profiles").update(data).eq("id", acc_id).execute()


def send_cw(room_id, account_id, message_id, text):
    headers = {"X-ChatWorkToken": CW_TOKEN}
    body = f"[rp aid={account_id} to={room_id}-{message_id}][pname:{account_id}]さん\n{text}"
    requests.post(
        f"https://api.chatwork.com/v2/rooms/{room_id}/messages",
        headers=headers,
        data={"body": body},
    )


def is_room_admin(room_id, account_id):
    headers = {"X-ChatWorkToken": CW_TOKEN}
    try:
        members = requests.get(
            f"https://api.chatwork.com/v2/rooms/{room_id}/members", headers=headers
        ).json()
        return any(
            str(m.get("account_id")) == str(account_id) and m.get("role") == "admin"
            for m in members
        )
    except:
        return False


def get_reply_target_id(body):
    """返信メッセージから [rp aid=XXXXX ...] のアカウントIDを取得する"""
    match = re.search(r"\[rp aid=(\d+)", body)
    return match.group(1) if match else None


def calc_tax(amount):
    if amount >= 10000:
        rate = 0.35
    elif amount >= 5000:
        rate = 0.20
    elif amount >= 1000:
        rate = 0.10
    else:
        rate = 0.05
    return max(1, int(amount * rate))

def delete_cw(room_id, message_id):
    """指定したメッセージを削除する"""
    headers = {"X-ChatWorkToken": CW_TOKEN}
    try:
        res = requests.delete(
            f"https://api.chatwork.com/v2/rooms/{room_id}/messages/{message_id}",
            headers=headers
        )
        return res.status_code == 200
    except:
        return False


def create_trade_room(item_name, item_url, buyer_id):
    headers = {"X-ChatWorkToken": CW_TOKEN}
    bot_id = get_bot_id()
    if not bot_id:
        return None, "Bot ID取得エラー"

    room_data = {
        "name": "【取引】",
        "description": f"商品: {item_name}\nURL: {item_url}\n購入者ID: {buyer_id}",
        "link": "1",
        "link_need_acceptance": "0",
        "members_admin_ids": bot_id,
        "members_member_ids": str(buyer_id),
    }

    try:
        r_res = requests.post(
            "https://api.chatwork.com/v2/rooms",
            headers=headers,
            data=room_data,
        ).json()

        new_rid = r_res.get("room_id")
        if not new_rid:
            return None, str(r_res.get("errors") or r_res.get("message") or r_res)

        l_res = requests.get(
            f"https://api.chatwork.com/v2/rooms/{new_rid}/link", headers=headers
        ).json()

        public_url = l_res.get("public_url") or l_res.get("url")
        if not public_url:
            return f"https://www.chatwork.com/#!rid{new_rid}", None

        return public_url, None

    except Exception as e:
        return None, str(e)


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

    if not acc_id or acc_id == get_bot_id():
        return Response(status=200)

    user = get_user(acc_id)
    if user.get("is_blacklisted"):
        return Response(status=200)

    is_admin = is_room_admin(room_id, acc_id)

    # /omikuji
    if body == "/omikuji":
        today = datetime.now().date().isoformat()
        if user.get("last_omikuji_at") == today:
            send_cw(room_id, acc_id, msg_id, "おみくじは1日1回です。")
        else:
            res = random.choice([
                {"n": "大吉", "p": 100},
                {"n": "吉", "p": 50},
                {"n": "凶", "p": 5},
            ])
            update_user(acc_id, {
                "points": (user.get("points") or 0) + res["p"],
                "last_omikuji_at": today,
            })
            send_cw(room_id, acc_id, msg_id, f"結果：{res['n']} ({res['p']}pt獲得)")
        return Response(status=200)

    # /lottery
    elif body == "/lottery" or body == "/lottery info":
        tickets = supabase.table("lottery_tickets").select("*").execute().data
        jackpot = len(tickets) * LOTTERY_TICKET_PRICE
        already = any(str(t["account_id"]) == acc_id for t in tickets)

        if body == "/lottery info" or body == "/lottery":
            # infoも購入コマンドも同じ画面を出してから購入判定
            info_msg = (
                f"[info][title]🎰 宝くじ[/title]"
                f"購入価格　: {LOTTERY_TICKET_PRICE}pt\n"
                f"当選確率　: {int(LOTTERY_WIN_RATE*100)}%\n"
                f"現在の購入者: {len(tickets)}人\n"
                f"賞金総額　: {jackpot}pt\n"
                f"抽選日　　: 毎週月曜0:00\n"
                f"─────────────\n"
                f"あなたの状態: {'購入済み ✅' if already else '未購入'}\n"
                f"購入: /lottery buy[/info]"
            )
            send_cw(room_id, acc_id, msg_id, info_msg)
        return Response(status=200)

    elif body == "/lottery buy":
        tickets = supabase.table("lottery_tickets").select("*").execute().data
        already = any(str(t["account_id"]) == acc_id for t in tickets)
        current_pts = user.get("points") or 0

        if already:
            send_cw(room_id, acc_id, msg_id, "今週はすでに購入済みです。")
        elif current_pts < LOTTERY_TICKET_PRICE:
            send_cw(room_id, acc_id, msg_id,
                f"ポイントが足りません。\n必要: {LOTTERY_TICKET_PRICE}pt / 所持: {current_pts}pt"
            )
        else:
            supabase.table("lottery_tickets").insert({"account_id": acc_id}).execute()
            update_user(acc_id, {"points": current_pts - LOTTERY_TICKET_PRICE})
            jackpot_new = (len(tickets) + 1) * LOTTERY_TICKET_PRICE
            send_cw(room_id, acc_id, msg_id,
                f"🎟️ 宝くじを購入しました！\n"
                f"─────────────\n"
                f"購入価格　: {LOTTERY_TICKET_PRICE}pt\n"
                f"残高　　　: {current_pts - LOTTERY_TICKET_PRICE}pt\n"
                f"現在の賞金総額: {jackpot_new}pt\n"
                f"抽選: 毎週月曜0:00 / 当選確率: {int(LOTTERY_WIN_RATE*100)}%"
            )
        return Response(status=200)
        referenced_msgs = re.findall(r"to=\d+-(\d+)", body)
        
        bot_id = get_bot_id()
        deleted_count = 0

        for target_msg_id in referenced_msgs:
            headers = {"X-ChatWorkToken": CW_TOKEN}
            try:
                msg_detail = requests.get(
                    f"https://api.chatwork.com/v2/rooms/{room_id}/messages/{target_msg_id}",
                    headers=headers
                ).json()
                
                if str(msg_detail.get("account", {}).get("account_id")) == bot_id:
                    if delete_cw(room_id, target_msg_id):
                        deleted_count += 1
            except:
                continue
        
        return Response(status=200)

    elif "/del" in body:
        referenced_msgs = re.findall(r"to=\d+-(\d+)", body)
        
        bot_id = get_bot_id()
        deleted_count = 0

        for target_msg_id in referenced_msgs:
            headers = {"X-ChatWorkToken": CW_TOKEN}
            try:
                msg_detail = requests.get(
                    f"https://api.chatwork.com/v2/rooms/{room_id}/messages/{target_msg_id}",
                    headers=headers
                ).json()
                
                if str(msg_detail.get("account", {}).get("account_id")) == bot_id:
                    if delete_cw(room_id, target_msg_id):
                        deleted_count += 1
            except:
                continue
        
        return Response(status=200)

    
    # /status
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
            chat_count = user.get("chat_count") or 0
            work_limit = get_work_limit(chat_count)
            limit_str = "無制限" if work_limit is None else f"{work_limit}回/日"

            # 次の段階まで何投稿必要か
            next_info = ""
            for threshold, limit in reversed(WORK_LIMIT_TIERS):
                if chat_count < threshold:
                    next_limit = "無制限" if limit is None else f"{limit}回/日"
                    next_info = f"\n📈 次の段階まで: あと{threshold - chat_count}投稿 → {next_limit}"
                    break

            send_cw(room_id, acc_id, msg_id,
                f"あなたのステータス\n"
                f"所持: {user.get('points')}pt\n"
                f"職業: {user.get('job')}\n"
                f"販売人: {user.get('is_seller')}\n"
                f"💬 今週の投稿数: {chat_count}{next_info}\n"
                f"⚒️ /work上限: {limit_str}（毎週月曜リセット）"
            )
        return Response(status=200)

    # /shop
    elif body == "/shop":
        items = supabase.table("items").select("*").execute().data
        msg = "[info][title]🏪 ショップ[/title]"
        msg += "".join([f"ID:{i['id']} {i['name']}({i['price']}pt)\n" for i in items])
        msg += "購入: /buy ID[/info]"
        send_cw(room_id, acc_id, msg_id, msg)
        return Response(status=200)

    # /buy
    elif body.startswith("/buy "):
        parts = body.split()
        if len(parts) >= 2:
            item_id = parts[1]
            item_res = supabase.table("items").select("*").eq("id", item_id).execute()
            if item_res.data:
                target = item_res.data[0]
                if (user.get("points") or 0) >= target["price"]:
                    link_url, errors = create_trade_room(
                        target["name"], target.get("url", "なし"), acc_id
                    )
                    if link_url:
                        update_user(acc_id, {"points": user["points"] - target["price"]})
                        send_cw(room_id, acc_id, msg_id,
                            f"購入完了！\n専用ルームはこちら:\n{link_url}"
                        )
                    else:
                        send_cw(room_id, acc_id, msg_id,
                            f"ルーム作成に失敗したため購入を中断しました。\nエラー: {errors}"
                        )
                else:
                    send_cw(room_id, acc_id, msg_id, "ポイントが足りません。")
        return Response(status=200)

    # /add_item
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
                        "seller_id": acc_id,
                    }).execute()
                    send_cw(room_id, acc_id, msg_id, f"成功: 「{item_name}」を登録しました。")
                except ValueError:
                    send_cw(room_id, acc_id, msg_id, "エラー: 価格は数値で。")
            else:
                send_cw(room_id, acc_id, msg_id, "使用法: /add_item 名前 価格 URL")
        return Response(status=200)

    # /job (一覧)
    elif body == "/job":
        jobs = supabase.table("jobs").select("*").execute().data
        msg = "[info][title]💼 職業一覧[/title]"
        msg += "".join([f"・{j['name']} (費用:{j['price']}pt)\n" for j in jobs])
        msg += "就職: /job 職業名[/info]"
        send_cw(room_id, acc_id, msg_id, msg)
        return Response(status=200)

    # /job 職業名
    elif body.startswith("/job "):
        name = body.replace("/job ", "").strip()
        job = supabase.table("jobs").select("*").eq("name", name).execute().data
        if job and (user.get("points") or 0) >= job[0]["price"]:
            update_user(acc_id, {"points": user["points"] - job[0]["price"], "job": name})
            send_cw(room_id, acc_id, msg_id, f"{name}に就職しました！")
        else:
            send_cw(room_id, acc_id, msg_id, "条件未達(pt不足など)")
        return Response(status=200)

    # /work
    elif body == "/work":
        job_data = supabase.table("jobs").select("*").eq("name", user.get("job")).execute().data
        if not job_data or user.get("job") == "なし":
            send_cw(room_id, acc_id, msg_id, "/job で就職してください。")
        else:
            now = datetime.now()
            today = now.date().isoformat()
            can_work = True
            if user.get("last_work_at"):
                try:
                    last_work = datetime.fromisoformat(
                        user["last_work_at"].replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                    diff = (now - last_work).total_seconds()
                    if diff < 1800:
                        send_cw(room_id, acc_id, msg_id,
                            f"休憩中(あと {int((1800 - diff) // 60)}分)"
                        )
                        can_work = False
                except:
                    pass
            if can_work:
                count = user.get("work_count", 0) if user.get("last_work_day") == today else 0
                work_limit = get_work_limit(user.get("chat_count") or 0)
                if work_limit is not None and count >= work_limit:
                    send_cw(room_id, acc_id, msg_id,
                        f"本日は{work_limit}回働きました。また明日！\n"
                        f"💬 チャット投稿数: {user.get('chat_count') or 0}pt"
                    )
                else:
                    reward = random.randint(job_data[0]["min_pt"], job_data[0]["max_pt"])
                    if random.random() < 0.05:
                        reward *= 2
                    update_user(acc_id, {
                        "points": (user.get("points") or 0) + reward,
                        "last_work_at": now.isoformat(),
                        "last_work_day": today,
                        "work_count": count + 1,
                    })
                    send_cw(room_id, acc_id, msg_id, f"{reward}pt 獲得！")
        return Response(status=200)

    # /daily_reset (管理者のみ)
    elif is_admin and body == "/daily_reset":
        try:
            supabase.table("profiles").update({
                "work_count": 0,
                "last_work_day": None,
                "last_omikuji_at": None,
                "last_hack_at": None,
                "last_steal_at": None,
            }).neq("id", "0").execute()
            send_cw(room_id, acc_id, msg_id, "デイリー制限をリセットしました。")
        except Exception as e:
            send_cw(room_id, acc_id, msg_id, f"エラー: {e}")
        return Response(status=200)

    # /give (累進税あり) — 返信型は body が [rp...] で始まるので行単位でチェック
    elif any(l.strip().startswith("/give") for l in body.splitlines()):
        # 返信型: /give [金額 or all] ← リプライ先のIDを自動取得
        # 通常型: /give [相手ID] [金額 or all]
        reply_target_id = get_reply_target_id(body)

        # /give コマンド行だけを抽出（返信時に付くヘッダー行を無視）
        give_line = next(
            (l.strip() for l in body.splitlines() if l.strip().startswith("/give")),
            ""
        )
        parts = give_line.split()

        # 返信型かどうか判定: /give の引数が1つ（金額のみ）かつリプライあり
        if reply_target_id and len(parts) == 2:
            target_id = reply_target_id
            amt_str = parts[1]
        elif len(parts) >= 3:
            target_id, amt_str = parts[1], parts[2]
        else:
            send_cw(room_id, acc_id, msg_id,
                "使用法:\n"
                "・返信して /give [金額 or all] — リプライ先に送金\n"
                "・/give [相手ID] [金額 or all] — ID指定で送金"
            )
            return Response(status=200)

        current_pts = user.get("points") or 0

        # allの場合は全所持ポイントを送金
        if amt_str == "all":
            # 税込で払える最大額を逆算: total_cost = amt + tax(amt) <= current_pts
            # 近似で二分探索
            lo, hi = 1, current_pts
            amt = 0
            while lo <= hi:
                mid = (lo + hi) // 2
                if mid + calc_tax(mid) <= current_pts:
                    amt = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            if amt <= 0:
                send_cw(room_id, acc_id, msg_id, "送金できるポイントがありません。")
                return Response(status=200)
        elif amt_str.isdigit():
            amt = int(amt_str)
        else:
            send_cw(room_id, acc_id, msg_id, "送金額は数値か all で指定してください。")
            return Response(status=200)

        tax = calc_tax(amt)
        total_cost = amt + tax

        if amt <= 0:
            send_cw(room_id, acc_id, msg_id, "送金額は1pt以上で指定してください。")
        elif target_id == acc_id:
            send_cw(room_id, acc_id, msg_id, "自分自身には送金できません。")
        elif current_pts < total_cost:
            send_cw(room_id, acc_id, msg_id,
                f"ポイントが足りません。\n"
                f"送金額: {amt}pt ＋ 税金: {tax}pt = 合計: {total_cost}pt 必要\n"
                f"現在の所持: {current_pts}pt"
            )
        else:
            target_user = get_user(target_id)
            update_user(acc_id, {"points": current_pts - total_cost})
            update_user(target_id, {"points": (target_user.get("points") or 0) + amt})

            if amt >= 10000:
                rate_label = "35%"
            elif amt >= 5000:
                rate_label = "20%"
            elif amt >= 1000:
                rate_label = "10%"
            else:
                rate_label = "5%"

            send_cw(room_id, acc_id, msg_id,
                f"[pname:{target_id}]さんに {amt}pt 送金しました\n"
                f"─────────────\n"
                f"送金額　: {amt}pt\n"
                f"税率　　: {rate_label}\n"
                f"税金　　: {tax}pt\n"
                f"合計支出: {total_cost}pt\n"
                f"残高　　: {current_pts - total_cost}pt"
            )
        return Response(status=200)
    
    
    # /help
    elif body == "/help":
        msg = (
            "[info][title]📖 コマンド一覧[/title]"
            "【基本】\n"
            "/omikuji\n"
            "　おみくじを引く。1日1回。結果に応じてpt獲得。\n"
            "　大吉:+100pt / 吉:+50pt / 凶:+5pt\n\n"
            "/status\n"
            "　自分のステータス(所持pt・職業・販売人フラグ)を確認。\n"
            "/status [ID]\n"
            "　指定ユーザーのステータスを確認。\n\n"
            "/ranking\n"
            "　ポイント上位10名を表示。\n\n"
            "【職業】\n"
            "/job\n"
            "　就ける職業の一覧と費用を表示。\n"
            "/job [職業名]\n"
            "　指定の職業に就く。費用ptが必要。\n\n"
            "/work\n"
            "　仕事をしてptを稼ぐ。30分ごと・1日最大10回。\n"
            "　5%の確率でボーナス2倍。職業に就いていないと使用不可。\n\n"
            "【ショップ・取引】\n"
            "/shop\n"
            "　販売中のアイテム一覧を表示。\n"
            "/buy [ID]\n"
            "　アイテムを購入。専用取引ルームが作成される。\n"
            "/add_item [名前] [価格] [URL]\n"
            "　アイテムを出品(販売人権限が必要)。\n\n"
            "【送金】\n"
            "/give [相手ID] [金額]\n"
            "/give [相手ID] all\n"
            "　相手にptを送金。累進税あり。\n"
            "　〜999pt:5% / 1000〜:10% / 5000〜:20% / 10000〜:35%\n"
            "　allで税引き後に送れる最大額を自動計算して送金。\n\n"
            "【銀行】\n"
            "/bank\n"
            "　銀行残高・未確定利息・財布を確認。\n"
            "/bank deposit [金額]\n"
            "　銀行に預ける。利息は0.5%/時間。\n"
            "/bank withdraw [金額 or all]\n"
            "　銀行から引き出す。利息込みで受け取れる。\n\n"
            "【職業専用】\n"
            "/hack [相手ID]\n"
            "　ハッカー専用。40%の確率で相手から50〜1000pt奪取。1日1回。\n"
            "/steal\n"
            "　泥棒専用。ランダムなユーザーから500〜1000pt奪取。1日1回。\n\n"
            "【宝くじ】\n"
            "/lottery\n"
            "　宝くじの情報確認（賞金総額・購入者数・抽選日）。\n"
            "/lottery buy\n"
            "　宝くじを購入(100pt)。1人1口。毎週月曜0:00に抽選。当選確率30%。\n\n"
            "【管理者専用】\n"
            "/reset weekly\n"
            "　月曜リセット(chat_count)を手動実行。ルーム管理者のみ。"
            "[/info]"
        )
        send_cw(room_id, acc_id, msg_id, msg)
        return Response(status=200)

    # /reset weekly (手動月曜リセット・管理者専用)
    elif body == "/reset weekly":
        if not is_room_admin(room_id, acc_id):
            send_cw(room_id, acc_id, msg_id, "このコマンドはルーム管理者のみ実行できます。")
            return Response(status=200)
        
        try:
            run_weekly_reset()
            send_cw(room_id, acc_id, msg_id,
                "✅ 週間リセット実行完了！\n"
                "chat_count をリセットしました。"
            )
        except Exception as e:
            send_cw(room_id, acc_id, msg_id, f"❌ エラーが発生しました: {str(e)}")
        return Response(status=200)

    # /ranking
    elif body == "/ranking":
        all_users = supabase.table("profiles").select("id, points").order("points", desc=True).limit(10).execute().data
        msg = "[info][title]🏆 ポイントランキング[/title]"
        medals = ["🥇", "🥈", "🥉"]
        for i, u in enumerate(all_users):
            prefix = medals[i] if i < 3 else f"{i+1}位"
            msg += f"{prefix} [pname:{u['id']}] {u['points']}pt\n"
        msg += "[/info]"
        send_cw(room_id, acc_id, msg_id, msg)
        return Response(status=200)

    # /bank
    elif body == "/bank" or body.startswith("/bank "):
        parts = body.split()

        # 残高確認
        if len(parts) == 1:
            bank_pts = user.get("bank_points") or 0
            deposited_at = user.get("bank_deposited_at")
            interest = 0
            if bank_pts > 0 and deposited_at:
                try:
                    dep_time = datetime.fromisoformat(deposited_at.replace("Z", "+00:00")).replace(tzinfo=None)
                    hours = (datetime.now() - dep_time).total_seconds() / 3600
                    interest = int(bank_pts * 0.005 * int(hours))
                except:
                    pass
            send_cw(room_id, acc_id, msg_id,
                f"銀行残高\n"
                f"─────────────\n"
                f"預入額　: {bank_pts}pt\n"
                f"未確定利息: +{interest}pt\n"
                f"（引き出し時に加算）\n"
                f"財布　　: {user.get('points') or 0}pt"
            )
            return Response(status=200)

        action = parts[1] if len(parts) >= 2 else ""
        amt_str = parts[2] if len(parts) >= 3 else ""

        # 預ける
        if action == "deposit":
            if not amt_str.isdigit():
                send_cw(room_id, acc_id, msg_id, "使用法: /bank deposit 金額")
            else:
                amt = int(amt_str)
                current_pts = user.get("points") or 0
                bank_pts = user.get("bank_points") or 0

                if amt <= 0:
                    send_cw(room_id, acc_id, msg_id, "1pt以上で指定してください。")
                elif current_pts < amt:
                    send_cw(room_id, acc_id, msg_id,
                        f"ポイントが足りません。\n財布: {current_pts}pt"
                    )
                else:
                    # 既に預けている場合、利息を確定してから追加
                    interest = 0
                    deposited_at = user.get("bank_deposited_at")
                    if bank_pts > 0 and deposited_at:
                        try:
                            dep_time = datetime.fromisoformat(deposited_at.replace("Z", "+00:00")).replace(tzinfo=None)
                            hours = (datetime.now() - dep_time).total_seconds() / 3600
                            interest = int(bank_pts * 0.005 * int(hours))
                        except:
                            pass

                    new_bank = bank_pts + interest + amt
                    update_user(acc_id, {
                        "points": current_pts - amt,
                        "bank_points": new_bank,
                        "bank_deposited_at": datetime.now().isoformat(),
                    })
                    send_cw(room_id, acc_id, msg_id,
                        f"預け入れ完了！\n"
                        f"─────────────\n"
                        f"預入額　: {amt}pt\n"
                        f"確定利息: +{interest}pt\n"
                        f"銀行残高: {new_bank}pt\n"
                        f"財布　　: {current_pts - amt}pt"
                    )

        # 引き出す
        elif action == "withdraw":
            if not amt_str.isdigit() and amt_str != "all":
                send_cw(room_id, acc_id, msg_id, "使用法: /bank withdraw 金額 or /bank withdraw all")
            else:
                bank_pts = user.get("bank_points") or 0
                deposited_at = user.get("bank_deposited_at")

                # 利息計算
                interest = 0
                if bank_pts > 0 and deposited_at:
                    try:
                        dep_time = datetime.fromisoformat(deposited_at.replace("Z", "+00:00")).replace(tzinfo=None)
                        hours = (datetime.now() - dep_time).total_seconds() / 3600
                        interest = int(bank_pts * 0.005 * int(hours))
                    except:
                        pass

                total_bank = bank_pts + interest
                amt = total_bank if amt_str == "all" else int(amt_str)

                if amt <= 0:
                    send_cw(room_id, acc_id, msg_id, "1pt以上で指定してください。")
                elif amt > total_bank:
                    send_cw(room_id, acc_id, msg_id,
                        f"引き出せません。\n銀行残高(利息込): {total_bank}pt"
                    )
                else:
                    remaining = total_bank - amt
                    update_user(acc_id, {
                        "points": (user.get("points") or 0) + amt,
                        "bank_points": remaining,
                        "bank_deposited_at": datetime.now().isoformat() if remaining > 0 else None,
                    })
                    send_cw(room_id, acc_id, msg_id,
                        f"引き出し完了！\n"
                        f"─────────────\n"
                        f"引出額　: {amt}pt\n"
                        f"確定利息: +{interest}pt\n"
                        f"銀行残高: {remaining}pt\n"
                        f"財布　　: {(user.get('points') or 0) + amt}pt"
                    )
        else:
            send_cw(room_id, acc_id, msg_id,
                "使用法:\n"
                "/bank — 残高確認\n"
                "/bank deposit 金額 — 預ける\n"
                "/bank withdraw 金額 — 引き出す\n"
                "/bank withdraw all — 全額引き出す"
            )
        return Response(status=200)

    # /hack (ハッカー専用)
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
                        update_user(target_id, {
                            "points": max(0, (target_user.get("points") or 0) - 100)
                        })
                        update_user(acc_id, {"points": (user.get("points") or 0) + reward})
                        send_cw(room_id, acc_id, msg_id, f"成功！{reward}pt獲得")
                    else:
                        send_cw(room_id, acc_id, msg_id, "失敗...")
        return Response(status=200)

    # /steal (泥棒専用)
    elif body == "/steal":
        if user.get("job") != "泥棒":
            send_cw(room_id, acc_id, msg_id, "泥棒専用です。")
        else:
            today = datetime.now().date().isoformat()
            if user.get("last_steal_at") == today:
                send_cw(room_id, acc_id, msg_id, "1日1回まで。")
            else:
                targets = (
                    supabase.table("profiles")
                    .select("*")
                    .neq("id", acc_id)
                    .gt("points", 0)
                    .execute()
                    .data
                )
                if targets:
                    t = random.choice(targets)
                    amt = min(t["points"], random.randint(500, 1000))
                    update_user(acc_id, {
                        "points": (user.get("points") or 0) + amt,
                        "last_steal_at": today,
                    })
                    update_user(t["id"], {"points": t["points"] - amt})
                    send_cw(room_id, acc_id, msg_id,
                        f"成功！[pname:{t['id']}]から{amt}pt奪取"
                    )
                else:
                    send_cw(room_id, acc_id, msg_id, "獲物がいません。")
        return Response(status=200)

    # コマンド以外のメッセージ: +1pt & chat_count+1
    update_user(acc_id, {
        "points": (user.get("points") or 0) + 1,
        "chat_count": (user.get("chat_count") or 0) + 1,
    })
    return Response(status=200)


# ═════════════════════════════════════════════════════════════════
# 外部Cron用エンドポイント（Vercel環境用）
# ═════════════════════════════════════════════════════════════════

@app.route("/cron/daily-reset", methods=["POST", "GET"])
def cron_daily_reset():
    """毎日0:00に外部Cronから呼び出し"""
    # セキュリティ: 環境変数のトークンをチェック
    cron_token = request.headers.get("Authorization")
    expected_token = os.environ.get("CRON_SECRET")
    
    if expected_token and cron_token != f"Bearer {expected_token}":
        return Response(json={"error": "Unauthorized"}, status=401)
    
    success = run_daily_reset()
    status = 200 if success else 500
    return Response(json={
        "status": "success" if success else "failed",
        "timestamp": datetime.now().isoformat()
    }, status=status)


@app.route("/cron/weekly-reset", methods=["POST", "GET"])
def cron_weekly_reset():
    """毎週月曜0:00に外部Cronから呼び出し"""
    # セキュリティ: 環境変数のトークンをチェック
    cron_token = request.headers.get("Authorization")
    expected_token = os.environ.get("CRON_SECRET")
    
    if expected_token and cron_token != f"Bearer {expected_token}":
        return Response(json={"error": "Unauthorized"}, status=401)
    
    success = run_lottery_draw()
    status = 200 if success else 500
    return Response(json={
        "status": "success" if success else "failed",
        "timestamp": datetime.now().isoformat()
    }, status=status)


if __name__ == "__main__":
    app.run()
