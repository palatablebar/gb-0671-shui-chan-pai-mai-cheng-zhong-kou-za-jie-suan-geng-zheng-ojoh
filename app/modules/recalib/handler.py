from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.web import render
router=APIRouter()
@router.get('/recalib')
def page(request:Request):
    return render(request,'recalib')
@router.post('/api/recalib')
def calculate(payload:dict=Body(default={})):
    return evaluate(payload,until=5)
