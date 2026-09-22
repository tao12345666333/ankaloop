# Data Science Workflow Example

This workflow demonstrates how multiple agents can collaborate on a data science project, from data analysis to model deployment and documentation.

> **Illustrative example:** All metrics, outputs, and readiness labels below are hypothetical
> conversation content. They are not AnkaLoop benchmark results or production-readiness claims.

## Workflow Overview

```
User Request → Data Scientist Agent → AI Engineer Agent → Documentation Writer Agent → Final Result
```

## Step-by-Step Workflow

### 1. Initial Request (User → Data Scientist)

**User Input:**
```
"I have a dataset of customer information and I want to build a machine learning model to predict customer churn. Please analyze the data, create an appropriate model, evaluate its performance, and provide deployment-ready code with comprehensive documentation."
```

**Data Scientist Agent Tasks:**
- Perform exploratory data analysis (EDA)
- Preprocess and clean the data
- Feature engineering and selection
- Split data into training and testing sets
- Choose appropriate ML algorithms
- Train and evaluate multiple models
- Perform hyperparameter tuning
- Select the best performing model

### 2. Model Optimization (Data Scientist → AI Engineer)

**Delegation Prompt:**
```
"In this hypothetical example, I've developed a customer churn prediction model with an
illustrative 85% accuracy. Please help me optimize it for production deployment, including model
optimization, API creation, and MLOps setup."
```

**AI Engineer Agent Tasks:**
- Optimize model for inference speed
- Create production-ready API endpoints
- Set up model monitoring and logging
- Implement model versioning
- Create Docker container for deployment
- Set up CI/CD pipeline for model updates
- Implement A/B testing framework

### 3. Documentation Creation (Data Scientist → Documentation Writer)

**Delegation Prompt:**
```
"Please create comprehensive documentation for the customer churn prediction project, including data analysis methodology, model architecture, API documentation, and deployment guides."
```

**Documentation Writer Agent Tasks:**
- Document data preprocessing pipeline
- Create model architecture documentation
- Write API documentation with examples
- Create deployment and setup guides
- Document model performance metrics
- Create user guides and tutorials

### 4. Final Integration (Data Scientist)

**Final Tasks:**
- Integrate optimized model and API
- Validate documentation accuracy
- Create final project report
- Provide recommendations for improvements

## Example Implementation

### Data Scientist Agent Configuration

```yaml
name: data-scientist
description: "Specialized agent for data analysis and machine learning"
mode: "primary"
system_prompt: |
  You are a Data Scientist agent specializing in end-to-end ML projects:
  1. Perform thorough exploratory data analysis
  2. Apply proper data preprocessing techniques
  3. Choose appropriate ML algorithms for the problem
  4. Validate models using proper evaluation metrics
  5. Consider model interpretability and fairness
  6. Delegate optimization tasks to ai-engineer agent
  7. Delegate documentation to documentation-writer agent
tools: ["read_file", "write_file", "apply_patch", "bash", "grep", "think"]
can_delegate: true
max_steps: 60
```

### AI Engineer Agent Configuration

```yaml
name: ai-engineer
description: "AI/ML specialist for production deployment"
mode: "subagent"
system_prompt: |
  You are an AI Engineer agent specializing in MLOps and production deployment:
  1. Optimize models for production performance
  2. Create scalable API endpoints
  3. Implement proper monitoring and logging
  4. Set up model versioning and CI/CD
  5. Consider deployment infrastructure
  6. Implement security best practices
tools: ["read_file", "write_file", "apply_patch", "bash", "grep", "think"]
can_delegate: false
max_steps: 40
```

## Sample Code Examples

### Data Scientist Analysis

```python
# Exploratory Data Analysis
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix

# Load and analyze data
df = pd.read_csv('customer_data.csv')
print(df.info())
print(df.describe())

# Visualize churn distribution
plt.figure(figsize=(10, 6))
sns.countplot(data=df, x='churn')
plt.title('Customer Churn Distribution')
plt.show()

# Feature engineering
def preprocess_data(df):
    # Handle missing values
    df.fillna(df.mean(), inplace=True)

    # Encode categorical variables
    categorical_cols = df.select_dtypes(include=['object']).columns
    df = pd.get_dummies(df, columns=categorical_cols, drop_first=True)

    return df

# Train model
X = preprocess_data(df.drop('churn', axis=1))
y = df['churn']

X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

model = RandomForestClassifier(n_estimators=100, random_state=42)
model.fit(X_train, y_train)

# Evaluate model
y_pred = model.predict(X_test)
print(classification_report(y_test, y_pred))
```

### AI Engineer Optimization

```python
# Production-ready API with FastAPI
from fastapi import FastAPI, HTTPException
import joblib
import pandas as pd
import numpy as np
from pydantic import BaseModel

app = FastAPI(title="Customer Churn Prediction API")

class CustomerData(BaseModel):
    # Define customer features
    age: int
    monthly_charges: float
    tenure: int
    # ... other features

class PredictionResponse(BaseModel):
    churn_probability: float
    churn_prediction: bool
    confidence: float

# Load optimized model
model = joblib.load("optimized_churn_model.pkl")
preprocessor = joblib.load("preprocessor.pkl")

@app.post("/predict", response_model=PredictionResponse)
async def predict_churn(customer_data: CustomerData):
    try:
        # Convert to DataFrame
        df = pd.DataFrame([customer_data.dict()])

        # Preprocess
        df_processed = preprocessor.transform(df)

        # Predict
        churn_prob = model.predict_proba(df_processed)[0][1]
        churn_pred = churn_prob > 0.5
        confidence = max(churn_prob, 1 - churn_prob)

        return PredictionResponse(
            churn_probability=float(churn_prob),
            churn_prediction=bool(churn_pred),
            confidence=float(confidence)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/model-info")
async def model_info():
    return {
        "model_type": "RandomForestClassifier",
        "accuracy": 0.85,
        "features": list(model.feature_names_in_),
        "version": "1.0.0"
    }
```

## Sample Conversation Flow

```
User: I have a dataset of customer information and I want to build a machine learning model to predict customer churn...

Data Scientist: I'll help you build a comprehensive customer churn prediction model. Let me start by analyzing your dataset and performing exploratory data analysis.

[Data Scientist performs EDA, preprocessing, and model training]

Data Scientist: In this hypothetical scenario, the Random Forest model has an illustrative 85%
accuracy. Now I'll delegate the optimization and deployment tasks to our AI Engineer.

Data Scientist uses the `task` tool with `agent_type="focused_coder"` and `description="Please help me optimize this churn prediction model for production deployment..."`

AI Engineer: I'll optimize your model for production and create the necessary deployment infrastructure.

[AI Engineer optimizes model, creates API, sets up monitoring]

Data Scientist: Excellent! Now I'll create comprehensive documentation for the project.

Data Scientist uses the `task` tool with `agent_type="focused_coder"` and `description="Please create comprehensive documentation for the customer churn prediction project..."`

Documentation Writer: I'll create thorough documentation for your churn prediction project.

[Documentation Writer creates API docs, user guides, etc.]

Data Scientist: Perfect! Your customer churn prediction system is now complete with optimized model, production API, and comprehensive documentation.
```

## Workflow Benefits

1. **End-to-End Solution**: Complete pipeline from data to deployment
2. **Deployment-oriented example**: Illustrates optimization steps for real-world usage
3. **Comprehensive Documentation**: Self-contained with guides
4. **Quality Assurance**: Multiple validation points
5. **Scalable Architecture**: Ready for production scaling

## Monitoring and Maintenance

### Model Monitoring
- Track prediction drift
- Monitor data distribution changes
- Alert on performance degradation
- Automated retraining triggers

### API Monitoring
- Response time tracking
- Error rate monitoring
- Request volume analytics
- Resource utilization tracking

## Usage Instructions

1. **Start the Data Scientist agent:**
   ```bash
   anka --agent examples/agents/data-scientist.yaml
   ```

2. **Ensure supporting agents are available:**
   ```bash
   cp examples/agents/documentation-writer.yaml ~/.config/ankaloop/agents/
   ```

3. **Provide your dataset and requirements**

   Delegation happens through the built-in `task` tool when the active agent has `can_delegate: true`.

This workflow showcases how AnkaLoop can handle complex data science projects with specialized agent collaboration.
