import os
import time
import requests
import re
import random
from fastapi import FastAPI, Request, Response

app = FastAPI()

CW_TOKEN = os.getenv("CW_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

points_cache = {}
last_post_time = {}
blacklisted_users = set()

def send_cw(room_id, account_id, message_id, text):
    url = f"https://api.chatwork.com/v2/rooms/{room_id}/messages"
    headers = {"X-ChatWorkToken": CW_TOKEN}
    
    formatted_body = (
        f"[rp aid={account_id} to={room_id}-{message_id}]"
        f"[piconname:{account_id}]さん\n{text}"
    )
    
    requests.post(url, headers=headers, data={"body": formatted_body})

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    
    event = data.get("webhook_event", {})
    acc_id = str(event.get("account_id"))
    room_id = event.get("room_id")
    msg_id = event.get("message_id")
    body = event.get("body", "").strip()
    now = time.time()

    if acc_id in blacklisted_users:
        return Response(status_code=200)

    prev_time = last_post_time.get(acc_id, 0)
    if now - prev_time < 0.5:
        blacklisted_users.add(acc_id)
        send_cw(room_id, acc_id, msg_id, "スパム検知：アカウントを凍結しました。")
        return Response(status_code=200)
    last_post_time[acc_id] = now

    if body.startswith("/give "):
        match = re.search(r"/give\s+(\d+)\s+(\d+)", body)
        if match:
            target_id, amount = match.groups()
            amount = int(amount)
            current_pt = points_cache.get(acc_id, 0)
            
            if amount <= 0 or acc_id == target_id:
                send_cw(room_id, acc_id, msg_id, "無効な操作です。")
            elif current_pt < amount:
                send_cw(room_id, acc_id, msg_id, f"pt不足です（現在: {current_pt}pt）")
            else:
                points_cache[acc_id] = current_pt - amount
                points_cache[target_id] = points_cache.get(target_id, 0) + amount
                send_cw(room_id, acc_id, msg_id, f"[pbid:{target_id}]へ {amount}pt 送金しました！")
        else:
            send_cw(room_id, acc_id, msg_id, "形式エラー：/give [ID] [枚数] と入力してください。")

    elif body.startswith("/dice "):
        match = re.search(r"/dice\s+(\d+)", body)
        if match:
            bet = int(match.group(1))
            current_pt = points_cache.get(acc_id, 0)

            if bet <= 0 or current_pt < bet:
                send_cw(room_id, acc_id, msg_id, "ptが足りないか、入力が不正です。")
            else:
                dice = [random.randint(1, 6) for _ in range(4)]
                dice_str = " ".join([f"[{d}]" for d in dice])
                if len(set(dice)) == 1:
                    win_pt = int(bet * 1.5)
                    points_cache[acc_id] += win_pt
                    send_cw(room_id, acc_id, msg_id, f"出目: {dice_str}\n🎉ゾロ目！当たりです！ +{win_pt}pt")
                else:
                    points_cache[acc_id] -= bet
                    send_cw(room_id, acc_id, msg_id, f"出目: {dice_str}\n残念...ハズレです。 -{bet}pt")

    elif body == "/status":
        pt = points_cache.get(acc_id, 0)
        send_cw(room_id, acc_id, msg_id, f"現在の所持ポイント: {pt} pt")

    elif body.startswith("/unban ") and acc_id == ADMIN_ID:
        target_id = body.replace("/unban ", "").strip()
        if target_id in blacklisted_users:
            blacklisted_users.remove(target_id)
            send_cw(room_id, acc_id, msg_id, f"ID:{target_id} の凍結を解除しました。")

    else:
        points_cache[acc_id] = points_cache.get(acc_id, 0) + 1
        if points_cache[acc_id] % 100 == 0:
            send_cw(room_id, acc_id, msg_id, f"🎉おめでとうございます！{points_cache[acc_id]}pt到達です！")

    return Response(status_code=200)
