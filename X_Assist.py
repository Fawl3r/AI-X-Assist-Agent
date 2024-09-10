import tweepy
import schedule
import time
import requests
import os
import logging
import json
from flask import Flask
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta
import random
import nltk
from nltk.sentiment.vader import SentimentIntensityAnalyzer
import spacy
from cachetools import TTLCache
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler("tweet_log.log", encoding='utf-8'),
                        logging.StreamHandler()
                    ])

# Twitter API credentials (replace placeholders with actual credentials)
bearer_token = os.getenv('BEARER_TOKEN')
consumer_key = os.getenv('CONSUMER_KEY')
consumer_secret = os.getenv('CONSUMER_SECRET')
access_token = os.getenv('ACCESS_TOKEN')
access_token_secret = os.getenv('ACCESS_TOKEN_SECRET')

# OpenAI API key (replace placeholders with actual API key)
openai_api_key = os.getenv('OPENAI_API_KEY')

# Initialize Twitter API client using tweepy
client = tweepy.Client(bearer_token=bearer_token,
                       consumer_key=consumer_key,
                       consumer_secret=consumer_secret,
                       access_token=access_token,
                       access_token_secret=access_token_secret,
                       wait_on_rate_limit=True)

# Initialize sentiment analyzer and spaCy
nltk.download('vader_lexicon')
sid = SentimentIntensityAnalyzer()
nlp = spacy.load("en_core_web_sm")

# Global flag for review mode
review_mode = False  # Set to False for automatic posting without review

# List of users to learn from (Add target users to analyze behavior)
learning_users = ['@User1', '@User2']  # Replace with actual usernames to learn from

# List of users to monitor and respond to (Add target users to respond to)
specified_users = ['@User3', '@User4']  # Replace with actual usernames to respond to

# JSON file to store learned features
learned_features_file = 'learned_features.json'
response_cache_file = 'response_cache.json'  # File to save the response cache

# Cache setup for response and tweet limits
response_cache = TTLCache(maxsize=1000, ttl=86400)  # Store up to 1000 tweet IDs for 24 hours

# Function to save cache to a file
def save_cache_to_file(cache, filename=response_cache_file):
    try:
        with open(filename, 'w') as f:
            json.dump(list(cache.keys()), f)  # Save only the tweet IDs
        logging.info("Response cache saved to file.")
    except Exception as e:
        logging.error(f"Error saving response cache to file: {e}")

# Function to load cache from a file
def load_cache_from_file(filename=response_cache_file):
    try:
        with open(filename, 'r') as f:
            tweet_ids = json.load(f)
            cache = TTLCache(maxsize=1000, ttl=86400)
            for tweet_id in tweet_ids:
                cache[tweet_id] = True
            logging.info("Response cache loaded from file.")
            return cache
    except Exception as e:
        logging.error(f"Error loading response cache from file: {e}")
        return TTLCache(maxsize=1000, ttl=86400)  # Return a new cache if error

# Analyze tweet features: track style, tone, response time, and sentiment
def analyze_tweet_features(tweets):
    features = {
        'sentiment': 'neutral',
        'common_words': '',
        'avg_sentence_length': 0,
        'punctuation': '',
        'preferred_response_time': 0
    }
    sentiments = []
    all_words = []
    sentence_lengths = []
    punctuation_usage = {'exclamation': 0, 'question': 0, 'comma': 0, 'period': 0}
    response_times = []

    current_time = datetime.now(timezone.utc)

    for tweet in tweets:
        sentiment = sid.polarity_scores(tweet.text)
        sentiments.append(sentiment['compound'])

        # NLP to analyze sentence structure
        doc = nlp(tweet.text)
        word_list = [token.text for token in doc if token.is_alpha]
        sentence_lengths.append(len(word_list))  # Track sentence length
        all_words.extend(word_list)

        # Check punctuation usage
        punctuation_usage['exclamation'] += tweet.text.count('!')
        punctuation_usage['question'] += tweet.text.count('?')
        punctuation_usage['comma'] += tweet.text.count(',')
        punctuation_usage['period'] += tweet.text.count('.')

        # Track response time (not applicable here, but kept for consistency)
        tweet_time = tweet.created_at
        response_times.append((current_time - tweet_time).total_seconds())

    # Average sentiment calculation
    if sentiments:
        avg_sentiment = sum(sentiments) / len(sentiments)
        if avg_sentiment >= 0.6:
            features['sentiment'] = 'very positive'
        elif 0.2 <= avg_sentiment < 0.6:
            features['sentiment'] = 'positive'
        elif -0.2 <= avg_sentiment < 0.2:
            features['sentiment'] = 'neutral'
        elif -0.6 <= avg_sentiment < -0.2:
            features['sentiment'] = 'negative'
        else:
            features['sentiment'] = 'very negative'

    # Calculate common words, average sentence length, and punctuation style
    if all_words:
        most_common_words = nltk.FreqDist(all_words).most_common(5)
        features['common_words'] = ', '.join([word for word, count in most_common_words])

    if sentence_lengths:
        features['avg_sentence_length'] = sum(sentence_lengths) / len(sentence_lengths)

    features['punctuation'] = max(punctuation_usage, key=punctuation_usage.get)

    return features

# Function to learn from specified users and store learned features
def learn_from_users(api, learned_features):
    logging.info("Learning from users...")
    for user in learning_users:
        try:
            user_data = api.get_user(username=user[1:])  # Remove '@' from username
            user_id = user_data.data.id

            # Fetch recent tweets from the user
            tweets_response = api.get_users_tweets(user_id, max_results=50, tweet_fields=['created_at', 'text', 'in_reply_to_user_id'])

            if tweets_response.data:
                replies = [tweet for tweet in tweets_response.data if tweet.in_reply_to_user_id is not None]

                if replies:
                    features = analyze_tweet_features(replies)
                    learned_features[user] = features
                    logging.info(f"Learned features from replies for {user}: {features}")
                else:
                    logging.warning(f"No replies found for {user} during learning process.")

        except Exception as e:
            logging.error(f"Error learning from {user}: {e}")

    # Save learned features to JSON file
    try:
        with open(learned_features_file, 'w', encoding='utf-8') as file:
            json.dump(learned_features, file, ensure_ascii=False, indent=4)
            logging.info("Learned features saved to file.")
    except Exception as e:
        logging.error(f"Error saving learned features to file: {e}")

# AI Response Generation Function (Feel free to adjust the tone and style)
def generate_ai_response(prompt, user_style, time_of_day):
    try:
        headers = {
            "Authorization": f"Bearer {openai_api_key}",
            "Content-Type": "application/json"
        }

        # Set response style based on time of day
        if 6 <= time_of_day <= 18:
            response_style = 'informal and conversational'
        else:
            response_style = 'direct and engaging'

        # Incorporate learned features into the system prompt
        system_prompt = f"You are an assistant. Respond in a short, conversational, and engaging tone."
        if user_style:
            system_prompt += f" Mimic the writing style with an average sentence length of {user_style.get('avg_sentence_length', 8)} words, and use punctuation like {user_style.get('punctuation', 'period')}."

        # Limit the response length to 25-30 words for brevity
        data = {
            "model": "gpt-4",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 100,  # Limit response length to 30 tokens for short replies
            "temperature": 0.7,
            "top_p": 0.9
        }
        response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=data)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content']
    except requests.exceptions.RequestException as e:
        logging.error(f"Error generating AI response: {e}")
        return None

# Function to automatically respond to original tweets (no retweets or replies)
def respond_to_tweets(api, user, learned_features, response_style='informal'):
    try:
        user_data = api.get_user(username=user[1:])  # Get user info, remove '@'
        bot_user_data = api.get_me()  # Get the bot's own user data
        bot_user_id = bot_user_data.data.id  # Bot's user ID for comparison

        # Fetch recent tweets from the user
        response = api.get_users_tweets(user_data.data.id, max_results=15, tweet_fields=['created_at', 'text', 'referenced_tweets', 'in_reply_to_user_id'])

        if response.data:
            current_time = datetime.now(timezone.utc)
            one_hour_ago = current_time - timedelta(hours=1)

            for tweet in response.data:
                tweet_time = tweet.created_at
                is_retweet = any(ref['type'] == 'retweeted' for ref in (tweet.referenced_tweets or [])) if tweet.referenced_tweets else False
                is_reply = tweet.in_reply_to_user_id is not None

                # Only respond to original tweets (not retweets or replies) no older than 1 hour
                if tweet_time > one_hour_ago and not is_retweet and not is_reply and tweet.id not in response_cache:
                    # Generate the response
                    prompt = f"Respond to this tweet in a short, {response_style} way: \"{tweet.text}\"."

                    # Get the learned features for style mimicry
                    user_style = learned_features.get(user, {})

                    ai_response = generate_ai_response(prompt, user_style, current_time.hour)

                    if ai_response:
                        try:
                            # Respond to the original tweet
                            api.create_tweet(
                                text=ai_response,
                                in_reply_to_tweet_id=tweet.id
                            )
                            logging.info(f"Responded to tweet ID {tweet.id} from {user} with: {ai_response}")

                            # Store the tweet ID in the cache to avoid responding again
                            response_cache[tweet.id] = True

                        except tweepy.TweepyException as e:
                            logging.error(f"Failed to respond to tweet ID {tweet.id} from {user}. Error: {e}")

                    time.sleep(60)  # Delay between responses to avoid rate limits

    except Exception as e:
        logging.error(f"Error fetching or responding to tweets for {user}: {e}")

# Main loop with enhanced error handling
if __name__ == "__main__":
    logging.info("Starting bot...")

    # Load previously learned features
    learned_features = {}
    logging.info("Loading learned features...")

    # Ensure the learned features file exists
    if os.path.exists(learned_features_file):
        with open(learned_features_file, 'r', encoding='utf-8') as file:
            learned_features = json.load(file)
            logging.info(f"Learned features loaded: {learned_features}")
    else:
        logging.info("No learned features file found, starting fresh.")

    # Load response cache from file
    response_cache = load_cache_from_file()

    # First learning process upon starting
    learn_from_users(client, learned_features)

    # Schedule the learning process every 28 minutes after the first one
    schedule.every(10).minutes.do(learn_from_users, api=client, learned_features=learned_features)

    # Start main loop with error handling
    try:
        while True:
            # Run pending scheduled tasks
            schedule.run_pending()

            # Fetch and respond to tweets from specified users
            for user in specified_users:
                logging.info(f"Fetching and responding to tweets from {user}")
                respond_to_tweets(client, user, learned_features, response_style='informal')
                time.sleep(60)  # Adjust sleep to avoid rate limits

            logging.info("Sleeping for 1 Hour 30 minutes before starting the next loop.")
            time.sleep(5400)  # 1 hr 30-minute wait between cycles

    except Exception as e:
        logging.error(f"Error occurred during main loop: {e}")
    
    finally:
        # Save the response cache to file on exit
        save_cache_to_file(response_cache)
