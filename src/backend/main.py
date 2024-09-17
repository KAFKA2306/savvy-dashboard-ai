import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import yfinance as yf
import pandas as pd
import numpy as np
from fredapi import Fred
from dotenv import load_dotenv
from datetime import datetime, timedelta
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io
import openai
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

# Load environment variables
load_dotenv()

app = FastAPI()

# CORS設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API keys
try:
    fred = Fred(api_key=os.getenv("FRED_API_KEY"))
except Exception as e:
    print(f"Error initializing FRED API: {str(e)}")

try:
    openai.api_key = os.getenv("OPENAI_API_KEY")
except Exception as e:
    print(f"Error setting OpenAI API key: {str(e)}")

class AnalysisRequest(BaseModel):
    query: str
    days: Optional[int] = 365
    use_google_drive: Optional[bool] = False

class FinancialData(BaseModel):
    dates: List[str]
    values: List[float]
    symbol: str

class AnalysisResponse(BaseModel):
    chartData: List[dict]
    statistics: dict
    aiAnalysis: str
    google_drive_link: Optional[str] = None

@lru_cache(maxsize=100)
def get_stock_data(symbol: str, days: int) -> FinancialData:
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    data = yf.download(symbol, start=start_date, end=end_date)
    return FinancialData(
        dates=data.index.strftime('%Y-%m-%d').tolist(),
        values=data['Close'].tolist(),
        symbol=symbol
    )

@lru_cache(maxsize=100)
def get_fred_data(series_id: str, days: int) -> FinancialData:
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    data = fred.get_series(series_id, start_date, end_date)
    return FinancialData(
        dates=data.index.strftime('%Y-%m-%d').tolist(),
        values=data.tolist(),
        symbol=series_id
    )

def perform_financial_analysis(data: FinancialData) -> dict:
    values = np.array(data.values)
    
    analysis = {
        "mean": np.mean(values),
        "std": np.std(values),
        "min": np.min(values),
        "max": np.max(values),
        "last_value": values[-1],
        "pct_change": (values[-1] / values[0] - 1) * 100 if len(values) > 1 else 0
    }
    
    return analysis

def save_to_google_drive(data: FinancialData) -> Optional[str]:
    try:
        creds = Credentials.from_authorized_user_file('token.json', ['https://www.googleapis.com/auth/drive.file'])
        service = build('drive', 'v3', credentials=creds)

        file_metadata = {'name': f'{data.symbol}_data.csv'}
        media = MediaIoBaseUpload(io.StringIO('\n'.join([f"{date},{value}" for date, value in zip(data.dates, data.values)])),
                                  mimetype='text/csv',
                                  resumable=True)
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return f"https://drive.google.com/file/d/{file.get('id')}/view"
    except Exception as e:
        print(f"Error saving to Google Drive: {str(e)}")
        return None

def analyze_with_ai(query: str, data: FinancialData) -> str:
    prompt = f"Analyze the following financial data for {data.symbol}:\n\n"
    prompt += f"Dates: {data.dates[:5]}...{data.dates[-5:]}\n"
    prompt += f"Values: {data.values[:5]}...{data.values[-5:]}\n\n"
    prompt += f"User query: {query}\n"
    prompt += "Provide a concise analysis based on the data and the user's query."

    response = openai.Completion.create(
        engine="text-davinci-002",
        prompt=prompt,
        max_tokens=150
    )

    return response.choices[0].text.strip()

@app.post("/analyze", response_model=AnalysisResponse)
async def analyze_financial_data(request: AnalysisRequest):
    try:
        # Simple parsing of the query
        if "stock" in request.query.lower():
            symbol = request.query.split()[-1].upper()
            data = get_stock_data(symbol, request.days)
        elif "fred" in request.query.lower():
            series_id = request.query.split()[-1]
            data = get_fred_data(series_id, request.days)
        else:
            raise ValueError("Unable to interpret the query")

        with ThreadPoolExecutor() as executor:
            analysis_future = executor.submit(perform_financial_analysis, data)
            ai_analysis_future = executor.submit(analyze_with_ai, request.query, data)
            
            analysis = analysis_future.result()
            ai_analysis = ai_analysis_future.result()

        chart_data = [{"date": date, "value": value} for date, value in zip(data.dates, data.values)]

        google_drive_link = None
        if request.use_google_drive:
            google_drive_link = save_to_google_drive(data)

        return AnalysisResponse(
            chartData=chart_data,
            statistics=analysis,
            aiAnalysis=ai_analysis,
            google_drive_link=google_drive_link
        )

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
