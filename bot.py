import os
import requests
from google import genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
import re
import time
from flask import Flask, request, jsonify

# Flask აპლიკაცია სოციალური ქსელებისა და გადახდის ვებჰუკებისთვის
app = Flask(__name__)

# Google Sheets კავშირის გამართვა
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client_sheets = gspread.authorize(creds)

spreadsheet = client_sheets.open("შეკვეთები")
sheet_orders = spreadsheet.sheet1
sheet_products = spreadsheet.worksheet("პროდუქტები")

# Gemini კლიენტის ინიციალიზაცია
client = genai.Client(
    api_key="AQ.Ab8RN6IBGOs07ba6hcC_m03FmHeJUnfFxgjRy6Jb5Lp-U3QsIg"
)

# Rate limiting დაცვისთვის
last_request_time = 0
REQUEST_INTERVAL = 1.0

# ბოლო შეკვეთების მეხსიერება დუბლიკატების ასაცილებლად
recent_orders_cache = {}

# გრძელვადიანი სესიის მეხსიერება
user_session_memory = {}

# Facebook / Instagram API-ს მონაცემები
PAGE_ACCESS_TOKEN = "EAAPBXIhvNegBSbtewg5QDD3TNZB89kc520wGPyIEGri4L5qINVUAMsY5pRjaBjHZCBrh4WoZCbM5RuFfRZBOGZCyO7RegYb7CuRNTuHlRcRMjUYzPpSkevZBnZBI3Krmt2lTtHlRn0OzTruiH4oxZAI2u6IuWrWFLe2dUtPIxYEiFtQ54BCUR24udNSa0ZBOchrvVfZCYCzgZDZD"
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID"

def send_facebook_message(recipient_id, message_text, image_url=None):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    headers = {"Content-Type": "application/json"}
    
    if image_url:
        payload = {
            "recipient": {"id": recipient_id},
            "message": {
                "attachment": {
                    "type": "image",
                    "payload": {"url": image_url, "is_reusable": True}
                }
            }
        }
        try:
            requests.post(url, json=payload, headers=headers)
        except Exception as e:
            print(f"ფოტოს გაგზავნის შეცდომა: {e}")

    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text}
    }
    try:
        response = requests.post(url, json=payload, headers=headers)
        return response.json()
    except Exception as e:
        print(f"მესიჯის გაგზავნის შეცდომა: {e}")

def notify_human_operator(user_id, user_message):
    if TELEGRAM_BOT_TOKEN != "YOUR_TELEGRAM_BOT_TOKEN":
        tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        text = f"🚨 ყურადღება! მომხმარებელს ({user_id}) ესაჭიროება ცოცხალი მენეჯერი!\nმესიჯი: {user_message}"
        try:
            requests.post(tg_url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
        except Exception as e:
            print(f"ოპერატორის გაფრთხილების შეცდომა: {e}")

def clean_phone(phone_input):
    digits = re.sub(r'\D', '', str(phone_input))
    if len(digits) >= 9:
        return digits[-9:]
    return digits

def validate_phone_number(phone_input):
    cleaned = clean_phone(phone_input)
    if len(cleaned) == 9 and cleaned.startswith('5'):
        return True, cleaned
    return False, cleaned

def find_latest_order_row(phone_number):
    try:
        cleaned_target = clean_phone(phone_number)
        rows = sheet_orders.get_all_values()
        for idx in range(len(rows) - 1, 0, -1):
            row = rows[idx]
            if len(row) >= 3:
                row_phone = clean_phone(row[2])
                if row_phone == cleaned_target:
                    return idx + 1
    except Exception:
        pass
    return None

def get_products_from_sheet():
    try:
        rows = sheet_products.get_all_values()
        if len(rows) <= 1:
            return "პროდუქტების ბაზა ცარიელია."
        
        db_text = "პროდუქტების ზუსტი სია:\n"
        for row in rows[1:]:
            if len(row) >= 5 and row[0].strip():
                prod_name = row[0].strip()
                price = row[1].strip()
                sizes = row[2].strip()
                stock = row[3].strip()
                desc = row[4].strip()
                db_text += f"- პროდუქტი: {prod_name} | ფასი: {price} ლარი | ზომები: {sizes} | მარაგი: {stock} | აღწერა: {desc}\n"
        return db_text
    except Exception as e:
        return "პროდუქტების ბაზა დროებით მიუწვდომელია."

def check_existing_customer(phone_number):
    try:
        row_idx = find_latest_order_row(phone_number)
        if row_idx:
            row_values = sheet_orders.row_values(row_idx)
            if len(row_values) >= 2:
                return row_values[1]
    except Exception:
        pass
    return None

def check_order_status_by_phone(phone_number):
    try:
        row_idx = find_latest_order_row(phone_number)
        if row_idx:
            row_values = sheet_orders.row_values(row_idx)
            status = row_values[6] if len(row_values) > 6 else "უცნობი"
            prod = row_values[4] if len(row_values) > 4 else ""
            return f"თქვენი შეკვეთა ({prod}) იძებნება. მიმდინარე სტატუსია: «{status}»."
        else:
            return "მითითებული ტელეფონის ნომრით აქტიური შეკვეთა ვერ მოიძებნა."
    except Exception as e:
        return "სტატუსის გადამოწმება ვერ მოხერხდა."

def is_duplicate_order(phone, order_text):
    now = datetime.now()
    if phone in recent_orders_cache:
        last_time, last_text = recent_orders_cache[phone]
        if now - last_time < timedelta(minutes=5) and last_text == order_text:
            return True
    recent_orders_cache[phone] = (now, order_text)
    return False

def update_stock_after_order(order_text):
    try:
        prod_rows = sheet_products.get_all_values()
        for idx in range(1, len(prod_rows)):
            row = prod_rows[idx]
            if len(row) >= 4 and row[0].strip().lower() in order_text.lower():
                current_stock_str = row[3].strip()
                if current_stock_str.isdigit():
                    current_stock = int(current_stock_str)
                    if current_stock > 0:
                        sheet_products.update_cell(idx + 1, 4, current_stock - 1)
    except Exception as e:
        print(f"მარაგის განახლების შეცდომა: {e}")

system_instruction = """
შენ ხარ ტანსაცმლისა და ფეხსაცმლის ონლაინ მაღაზიის თავაზიანი და ყურადღიანი კონსულტანტი ინსტაგრამზე. 

მიტანის პირობები:
- თბილისი: მიტანა უფასოა / ხდება 1-2 სამუშაო დღეში.
- რეგიონები: მიტანის საფასურია 7 ლარი / ხდება 2-4 სამუშაო დღეში.

მაღაზიის პოლიტიკა:
- თუ კლიენტი აუქმებს შეკვეთას და თანხა გადახდილი ჰქონდა ბარათით, აუხსენი, რომ თანხა ბარათზე ავტომატურად/მენეჯერის მიერ დაუბრუნდება 1-2 სამუშაო დღეში.
- შეკვეთა ცხრილში არ იშლება, არამედ სტატუსი იცვლება „გაუქმებულია“-ზე.

მკაცრი წესები:
1. ელაპარაკე მომხმარებელს ბუნებრივი, ცოცხალი და თავაზიანი ქართულით.
2. **პროდუქტის სახელის მკაცრი დაცვა:** როდესაც ასახელებ პროდუქტს, გამოიყენე ზუსტად ის სახელი, რაც ცხრილში წერია (მაგალითად: „ჰუდი“, „ბოტასები“, „ჩუსტები“, „ხალათი“). არასოდეს შეცვალო ან აურიო ისინი ერთმანეთში!
3. **ფოტოების მოთხოვნა:** თუ კლიენტი ითხოვს ფოტოს, პასუხის ბოლოს დაურთე ტეგი: [SEND_PHOTO: პროდუქტის_სახელი]
4. **ზომისა და მარაგის მკაცრი კონტროლი:** აუცილებლად შესთავაზე კლიენტს ბაზაში არსებული ზომები. არ დააფიქსირო შეკვეთა, სანამ ზომა მარაგში არ გადამოწმდება.
5. **ტელეფონის მკაცრი ფორმატი:** მკაცრად მოითხოვე ზუსტად 9-ნიშნა ტელეფონის ნომერი, რომელიც იწყება 5-იანით (მაგ: 599123456) მხოლოდ მაშინ, როცა შეკვეთის გაფორმების საბოლოო ეტაპზე მიხვალთ.
6. **შეკვეთის სტატუსი:** თუ კლიენტი ითხოვს შეკვეთის სტატუსს, პასუხის ბოლოს დაურთე ტეგი: [CHECK_STATUS: ტელეფონის_ნომერი]
7. როცა კლიენტი გადაწყვეტს ყიდვას, ეტაპობრივად გამოჰკითხე და ზუსტად ჩაიწერე: პროდუქტი და ზომა, სახელი და გვარი, ტელეფონი (9-ნიშნა, 5-იანიდან), მისამართი და სასურველი მიტანის დრო, გადახდის მეთოდი.
8. ბარათით არჩევისას მიაწოდე ლინკი: https://pay.ge/sample-shop-link
9. დადასტურების შემდეგ, პასუხის ბოლოს აუცილებლად დაურთე ტეგი: 
    [SAVE_ORDER: პროდუქტი(ზომა), სახელი, ტელეფონი, მისამართი (დრო), გადახდის_მეთოდი]
10. **გაუქმება, შეცვლა ან ოპერატორი:** 
    - [CANCEL_ORDER: ტელეფონის_ნომერი]
    - [UPDATE_ORDER: ტელეფონის_ნომერი, ახალი_მისამართი]
    - [HUMAN_NEEDED] (გამოიყენე, თუ კლიენტმა მოითხოვა ცოცხალი მენეჯერი, გამოგზავნა გადახდის სქრინი/ქვითარი, ან რთული ინდივიდუალური კითხვა აქვს).
"""

def call_gemini_with_retry(full_prompt, max_retries=3):
    delay = 1.0
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=full_prompt
            )
            return response.text
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            time.sleep(delay)
            delay *= 2

def process_bot_logic(user_id, user_input, incoming_image_url=None):
    current_time = time.time()
    global last_request_time
    elapsed = current_time - last_request_time
    if elapsed < REQUEST_INTERVAL:
        time.sleep(REQUEST_INTERVAL - elapsed)
    last_request_time = time.time()
    
    current_products = get_products_from_sheet()
    
    if user_id not in user_session_memory:
        user_session_memory[user_id] = {"history": []}
    
    log_text = user_input
    if incoming_image_url:
        log_text += f" [მომხმარებელმა გამოგზავნა ფოტო/სქრინი: {incoming_image_url}]"

    user_session_memory[user_id]["history"].append(f"კლიენტი: {log_text}")
    session_history_text = "\n".join(user_session_memory[user_id]["history"][-5:])

    digits_in_text = re.sub(r'\D', '', user_input)
    customer_context = ""
    found_phone = ""
    if len(digits_in_text) >= 9:
        raw_found = digits_in_text[-9:]
        is_valid, cleaned_p = validate_phone_number(raw_found)
        if is_valid:
            found_phone = cleaned_p
            customer_name = check_existing_customer(found_phone)
            if customer_name:
                customer_context = f"\n[სისტემური ცნობა: ეს მომხმარებელია {customer_name}, ტელეფონი: {found_phone}. მიესალმე სახელით!]\n"

    image_prompt_addition = f"\n[ყურადღება: მომხმარებელმა მოწერა თანდართული ფოტოთი/სქრინით: {incoming_image_url}]\n" if incoming_image_url else ""

    full_prompt = f"""
{system_instruction}

[მიმდინარე პროდუქტების ბაზა Google Sheets-იდან:
{current_products}]
{customer_context}
{image_prompt_addition}
[მომხმარებელთან დიალოგის ბოლო ისტორია:
{session_history_text}]

კლიენტის ახალი შეტყობინება: {user_input}
"""
    
    try:
        reply_text = call_gemini_with_retry(full_prompt)
    except Exception as api_error:
        return "მოგესალმებით! ამჟამად მცირედი ტექნიკური შეფერხება გვაქვს, მალე გიპასუხებთ.", None

    photo_to_send = None

    if "[SEND_PHOTO:" in reply_text:
        start_idx = reply_text.find("[SEND_PHOTO:")
        end_idx = reply_text.find("]", start_idx)
        if end_idx != -1:
            prod_query = reply_text[start_idx+12:end_idx].strip().lower()
            reply_text = reply_text.replace(reply_text[start_idx:end_idx+1], "").strip()
            try:
                rows = sheet_products.get_all_values()
                for row in rows[1:]:
                    if len(row) >= 5 and prod_query in row[0].strip().lower():
                        pass
            except Exception:
                pass

    if "[CHECK_STATUS:" in reply_text:
        start_idx = reply_text.find("[CHECK_STATUS:")
        end_idx = reply_text.find("]", start_idx)
        if end_idx != -1:
            phone_data = reply_text[start_idx+14:end_idx].strip()
            status_msg = check_order_status_by_phone(phone_data)
            reply_text = reply_text.replace(reply_text[start_idx:end_idx+1], "").strip()
            reply_text += f"\n\n{status_msg}"

    elif "[SAVE_ORDER:" in reply_text:
        start_idx = reply_text.find("[SAVE_ORDER:")
        end_idx = reply_text.find("]", start_idx)
        if end_idx != -1:
            order_data = reply_text[start_idx+12:end_idx]
            parts = [p.strip() for p in order_data.split(',')]
            phone_extracted = parts[2] if len(parts) >= 3 else found_phone
            
            is_valid_phone, valid_phone_str = validate_phone_number(phone_extracted)
            
            if not is_valid_phone:
                reply_text = "უკაცრავად, მითითებული ტელეფონის ნომერი არასწორია. გთხოვთ მიუთითოთ 9-ნიშნა ნომერი, რომელიც იწყება 5-იანით (მაგ: 599123456)."
            elif is_duplicate_order(valid_phone_str, order_data):
                reply_text = "თქვენ უკვე დააფიქსირეთ ზუსტად ეს შეკვეთა რამდენიმე წუთის წინ. მენეჯერი მალე გიპასუხებთ!"
            else:
                save_order_to_google_sheet(order_data)
                reply_text = reply_text.replace(reply_text[start_idx:end_idx+1], "").strip()
                reply_text += "\n\n✅ შეკვეთა წარმატებით დაფიქსირდა და მარაგი განახლდა!"

    elif "[CANCEL_ORDER:" in reply_text:
        start_idx = reply_text.find("[CANCEL_ORDER:")
        end_idx = reply_text.find("]", start_idx)
        if end_idx != -1:
            phone_data = reply_text[start_idx+14:end_idx].strip()
            res_msg = cancel_order_in_sheet(phone_data)
            reply_text = reply_text.replace(reply_text[start_idx:end_idx+1], "").strip()
            reply_text += f"\n\n{res_msg}"

    elif "[UPDATE_ORDER:" in reply_text:
        start_idx = reply_text.find("[UPDATE_ORDER:")
        end_idx = reply_text.find("]", start_idx)
        if end_idx != -1:
            update_data = reply_text[start_idx+14:end_idx]
            parts = [p.strip() for p in update_data.split(',')]
            if len(parts) >= 2:
                res_msg = update_order_in_sheet(parts[0], parts[1])
                reply_text = reply_text.replace(reply_text[start_idx:end_idx+1], "").strip()
                reply_text += f"\n\n{res_msg}"

    elif "[HUMAN_NEEDED]" in reply_text:
        reply_text = reply_text.replace("[HUMAN_NEEDED]", "").strip()
        notify_human_operator(user_id, user_input)
        reply_text += "\n\n👨‍💻 თქვენი შეტყობინება გადაეგზავნა ცოცხალ მენეჯერს. ის მალე გიპასუხებთ!"

    user_session_memory[user_id]["history"].append(f"ბოტი: {reply_text}")
    return reply_text, photo_to_send

def save_order_to_google_sheet(order_details):
    current_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    status = "ახალი"
    parts = [p.strip() for p in order_details.split(',')]
    
    if len(parts) >= 5:
        order_text, name, phone, address, payment_method = parts[0], parts[1], parts[2], parts[3], parts[4]
        price = "მოთხოვნილია"
    elif len(parts) == 4:
        order_text, name, phone, address = parts[0], parts[1], parts[2], parts[3]
        payment_method = "კურიერთან ნაღდი"
        price = "მოთხოვნილია"
    else:
        order_text, name, phone, address, payment_method, price = order_details, "უცნობი", "უცნობი", "უცნობი", "უცნობი", "-"

    full_order_description = f"{order_text} (გადახდა: {payment_method})"
    sheet_orders.append_row([current_date, name, phone, address, full_order_description, price, status])
    update_stock_after_order(order_text)

def cancel_order_in_sheet(phone_number):
    try:
        row_idx = find_latest_order_row(phone_number)
        if row_idx:
            sheet_orders.update_cell(row_idx, 7, "გაუქმებულია")
            return f"შეკვეთა ტელეფონით ({phone_number}) გაუქმდა."
        else:
            return "შეკვეთა მითითებული ნომრით ვერ მოიძებნა."
    except Exception as e:
        return f"შეცდომა: {e}"

def update_order_in_sheet(phone_number, new_address):
    try:
        row_idx = find_latest_order_row(phone_number)
        if row_idx:
            sheet_orders.update_cell(row_idx, 4, new_address)
            sheet_orders.update_cell(row_idx, 7, "შეცვლილია")
            return f"მისამართი განახლდა."
        else:
            return "შეკვეთა ვერ მოიძებნა."
    except Exception as e:
        return f"შეცდომა: {e}"

@app.route('/payment-webhook', methods=['POST'])
def payment_webhook():
    data = request.get_json()
    try:
        phone = data.get('phone')
        status = data.get('status')
        if status == 'success' and phone:
            row_idx = find_latest_order_row(phone)
            if row_idx:
                sheet_orders.update_cell(row_idx, 7, "გადახდილია")
                return jsonify({"status": "updated"}), 200
    except Exception as e:
        print(f"Payment Webhook Error: {e}")
    return jsonify({"status": "failed"}), 400

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if mode and token:
            if token == "my_instagram_bot_secret_2026":
                return challenge, 200
            return "Forbidden", 403
        return "Hello World", 200

    elif request.method == 'POST':
        body = request.get_json()
        try:
            for entry in body.get('entry', []):
                for messaging in entry.get('messaging', []):
                    sender_id = messaging['sender']['id']
                    
                    message_text = ""
                    incoming_image_url = None

                    if 'message' in messaging:
                        msg_data = messaging['message']
                        if 'text' in msg_data:
                            message_text = msg_data['text']
                        
                        if 'attachments' in msg_data:
                            for attachment in msg_data['attachments']:
                                if attachment.get('type') == 'image':
                                    incoming_image_url = attachment.get('payload', {}).get('url')
                                    if not message_text:
                                        message_text = "გამოვგზავნე გადახდის ქვითარი / სქრინი"

                    if message_text or incoming_image_url:
                        bot_reply, photo_url = process_bot_logic(sender_id, message_text, incoming_image_url=incoming_image_url)
                        send_facebook_message(sender_id, bot_reply, image_url=photo_url)
                        
        except Exception as e:
            print(f"Webhook Error: {e}")
        return jsonify({"status": "success"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
