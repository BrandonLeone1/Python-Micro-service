from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from pydantic import BaseModel
import pandas as pd
import io
import csv
from openai import OpenAI
from typing import Optional, Literal
import psycopg
from dotenv import load_dotenv
import os

load_dotenv()
app = FastAPI()
openai_client = OpenAI(api_key=os.getenv("OPENAI_KEY"))

class openAIAnswer(BaseModel):
    metric_date: str
    clicks: str
    impressions: str
    conversions: str
    ad_spend: str
    revenue: Optional[str] = None
    platform: Literal["Google Ads", "Meta Ads", "TikTok Ads"]

@app.get("/")
def root():
    return {"Hello": "World"}

def connectDB():
    conn = psycopg.connect(
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT")
    )
    try:
        yield conn
    finally:
        conn.close()

@app.post("/upload-csv")
async def handle_upload(campaign_id: int, conn = Depends(connectDB), file: UploadFile = File(...)):
    
    if not file.filename.endswith(".csv"):
        raise HTTPException(400, "File must be a valid CSV")
    
    cursor = conn.cursor()

    header_index = None
    target_headers = {"day", "date", "campaign", "campaign id", "campaign name", "reporting start", "reporting end"}
    
    content = await file.read()
    lines = content.decode("utf-8").splitlines()

    if len(lines) > 5000:
        raise HTTPException(400, "Too big of a file.")
    reader = csv.reader(lines)
    
    for i, row in enumerate(reader):
        cleaned_columns = {col.strip().lower() for col in row}

        if cleaned_columns.intersection(target_headers):
            header_index = i
            break

    await file.seek(0)
    
    if header_index is None:
        raise HTTPException(400, "Couldn't find an index for headers")
    

    df = pd.read_csv(io.BytesIO(content), header=header_index, skip_blank_lines=False, nrows=2000)
    
    if len(df.columns) > 30:
        raise HTTPException(400, "Malformed CSV file: too many headers")
    
    raw_headers = list(df.columns)
    
    try:
        completion = openai_client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system", 
                    "content": "You are a data engineering assistant. Your job is to analyze a list of raw CSV header columns from a marketing platform (Meta Ads, Google Ads, or TikTok Ads) and map them to our internal standardized schema fields. Identify the platform based on the column names provided. Then return the names of the columns that map to ours. Follow the output model. **HINT**: if it mentions 2-second video views or 6-second video views, it is tiktok ads. ONLY RETURN TIKTOK ADS IF THESE COLUMNS EXIST. **HINT**: ANYTHING WITH 'Reporting Start' or 'Reporting End' is META. **HINT**: GOOGLE ADS WILL HAVE COLUMNS LIKE 'conversion value' OR 'Conv. value'. **Standardize your response to: 1) Google Ads 2) Meta Ads 3) TikTok Ads**"
                },
                {
                    "role": "user", 
                    "content": f"Here are the raw CSV headers: {raw_headers}. Map them to the required fields. Select the closest match from the list for each required key."
                },
            ],
            response_format=openAIAnswer,

        )
    except Exception:
        raise HTTPException(500, "OpenAI failed")
    
    mapping = completion.choices[0].message.parsed

    clean_df = pd.DataFrame()

    clean_df["metric_date"] = pd.to_datetime(df[mapping.metric_date]).dt.strftime('%Y-%m-%d')
    clean_df["clicks"] = pd.to_numeric(df[mapping.clicks].astype(str).str.replace("[^0-9]", "", regex=True).fillna(0))
    clean_df["impressions"] = pd.to_numeric(df[mapping.impressions].astype(str).str.replace("[^0-9]", "", regex=True).fillna(0))
    clean_df["conversions"] = pd.to_numeric(df[mapping.conversions].astype(str).str.replace("[^0-9]", "", regex=True).fillna(0))
    clean_df["ad_spend"] = pd.to_numeric(df[mapping.ad_spend].astype(str).str.replace("[^0-9.]", "", regex=True).fillna(0))
    clean_df["platform"] = mapping.platform

    if mapping.revenue is not None:
        clean_df["revenue"] = pd.to_numeric(df[mapping.revenue].astype(str).str.replace("[^0-9.]", "", regex=True).fillna(0))
    else:
        clean_df["revenue"] = 0.0
    clean_df["campaign_id"] = campaign_id
    print(clean_df)

    records_to_insert = list(clean_df[['campaign_id', 'metric_date', 'clicks', 'impressions', 'conversions', 'ad_spend', 'revenue', 'platform']].itertuples(index=False, name=None))
    insert_query = """
        INSERT INTO campaign_metrics (campaign_id, metric_date, clicks, impressions, conversions, ad_spend, revenue, platform)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """

    try:
        cursor.executemany(insert_query, records_to_insert)
        conn.commit()
    except Exception:
        conn.rollback()
        raise HTTPException(500, "Failed trying to insert records to DB")
    clean_df.to_csv(f"{mapping.platform}-data.csv", index=False)
    return mapping