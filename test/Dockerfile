FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies required for audio processing (libsndfile for soundfile)
RUN apt-get update && apt-get install -y libsndfile1 && rm -rf /var/lib/apt/lists/*

# Copy backend requirements first to leverage Docker cache
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project into the container
COPY backend /app/backend
COPY frontend /app/frontend

# Expose the port FastAPI will run on
EXPOSE 8000

# Set the working directory to backend where main.py exists
WORKDIR /app/backend

# Start the application using Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
