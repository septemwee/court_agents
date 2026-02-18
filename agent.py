import os
import logging
import google.cloud.logging
from dotenv import load_dotenv

from callback_logging import log_query_to_model, log_model_response

from google.adk import Agent
from google.adk.agents import SequentialAgent, ParallelAgent, LoopAgent
from google.adk.models import Gemini
from google.genai import types
from google.adk.tools import exit_loop
from google.adk.tools.tool_context import ToolContext
from google.adk.tools.langchain_tool import LangchainTool

from langchain_community.tools import WikipediaQueryRun
from langchain_community.utilities import WikipediaAPIWrapper


# ENVIRONMENT SETUP

cloud_logging_client = google.cloud.logging.Client()
cloud_logging_client.setup_logging()

load_dotenv()
MODEL_NAME = os.getenv("MODEL")
RETRY_OPTIONS = types.HttpRetryOptions(initial_delay=1, attempts=6)


#  TOOLS DEFINITION

def append_to_state(tool_context: ToolContext, field: str, response: str):
    existing = tool_context.state.get(field, "")
    if existing:
        tool_context.state[field] = existing + "\n\n" + response
    else:
        tool_context.state[field] = response
    return {"status": "success"}

def write_file(tool_context: ToolContext, filename: str, content: str):
    os.makedirs("court_agents/court_reports", exist_ok=True)
    safe_name = filename.replace(" ", "_")
    path = f"court_agents/court_reports/{safe_name}"

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"status": f"saved to {path}"}

wiki_tool = LangchainTool(
    tool=WikipediaQueryRun(api_wrapper=WikipediaAPIWrapper())
)


# STEP 2: INVESTIGATION (PARALLEL)

admirer = Agent(
    name="admirer",
    model=Gemini(model=MODEL_NAME, retry_options=RETRY_OPTIONS),
    description="ฝ่ายสนับสนุน รวบรวมข้อมูลด้านบวกและความสำเร็จ",
    instruction="""
        หัวข้อ: { PROMPT? }
        ข้อเสนอแนะจากผู้พิพากษา:
        { judge_feedback? }

        คำสั่ง:
        - ใช้ Wikipedia tool เท่านั้น
        - ค้นหาด้วยคำว่า:
            "<หัวข้อ> achievements"
            "<หัวข้อ> accomplishments"
            "<หัวข้อ> reforms"
            "<หัวข้อ> legacy"
        - หากมี feedback จาก judge ให้ปรับคำค้นหาให้เจาะลึกขึ้น

        เงื่อนไข:
        - ต้องเขียนอย่างน้อย 120 คำ
        - ห้ามกล่าวถึงข้อโต้แย้งหรือด้านลบ
        - เขียนเป็นย่อหน้าที่เป็นทางการ

        สุดท้าย:
        - ใช้ append_to_state
        - field = "pos_data"
    """,
    tools=[wiki_tool, append_to_state],
)

critic_researcher = Agent(
    name="critic_researcher",
    model=Gemini(model=MODEL_NAME, retry_options=RETRY_OPTIONS),
    description="ฝ่ายคัดค้าน รวบรวมข้อมูลด้านลบและข้อโต้แย้ง",
    instruction="""
        หัวข้อ: { PROMPT? }
        ข้อเสนอแนะจากผู้พิพากษา:
        { judge_feedback? }

        คำสั่ง:
        - ใช้ Wikipedia tool เท่านั้น
        - ค้นหาด้วยคำว่า:
            "<หัวข้อ> controversy"
            "<หัวข้อ> criticism"
            "<หัวข้อ> failures"
            "<หัวข้อ> human rights issues"
        - หากมี feedback จาก judge ให้ปรับคำค้นหาให้เจาะลึกขึ้น

        เงื่อนไข:
        - ต้องเขียนอย่างน้อย 120 คำ
        - ห้ามกล่าวถึงคำชม
        - เขียนเป็นย่อหน้าที่เป็นทางการ

        สุดท้าย:
        - ใช้ append_to_state
        - field = "neg_data"
    """,
    tools=[wiki_tool, append_to_state],
)

investigation_team = ParallelAgent(
    name="investigation_team",
    description="ทีมสืบสวนสองฝ่ายทำงานขนานกัน",
    sub_agents=[admirer, critic_researcher],
)

# STEP 3: TRIAL & REVIEW (LOOP)

judge = Agent(
    name="judge",
    model=Gemini(model=MODEL_NAME, retry_options=RETRY_OPTIONS),
    description="ผู้พิพากษาตรวจสอบความสมดุลของข้อมูล",
    instruction="""
        หัวข้อ: { PROMPT? }

        ข้อมูลด้านบวก:
        { pos_data? }

        ข้อมูลด้านลบ:
        { neg_data? }

        คำสั่งสำคัญ:
        1. ให้นับจำนวนคำจริงของแต่ละฝ่าย (word count จริง)
        2. ห้ามประมาณจำนวนคำ
        3. ถ้าฝ่ายใดต่ำกว่า 120 คำ ให้ถือว่าไม่ผ่าน

        เกณฑ์:
        - ทั้งสองฝ่ายต้องมีอย่างน้อย 120 คำ
        - เนื้อหาต้องมีรายละเอียด ไม่ซ้ำ และมีสาระเชิงวิเคราะห์

        ถ้ายังไม่ครบ:
        - อธิบายสิ่งที่ขาด
        - เขียน feedback ลงใน field "judge_feedback"
        - ห้ามใช้ exit_loop

        ถ้าครบและสมดุล:
        - ใช้ exit_loop tool เท่านั้น
        - ห้ามเขียนข้อความอื่นใด
    """,
    tools=[append_to_state, exit_loop],
    before_model_callback=log_query_to_model,
    after_model_callback=log_model_response,
)

trial_loop = LoopAgent(
    name="trial_room",
    description="กระบวนการไต่สวนแบบวนลูปจนข้อมูลสมบูรณ์",
    sub_agents=[investigation_team, judge],
    max_iterations=5,
)

# STEP 4: VERDICT WRITER

verdict_writer = Agent(
    name="verdict_writer",
    model=Gemini(model=MODEL_NAME, retry_options=RETRY_OPTIONS),
    description="สรุปคำพิพากษาเชิงประวัติศาสตร์อย่างเป็นกลาง",
    instruction="""
        หัวข้อ: ชื่อบุคคลหรือเหตุการณ์ที่ user ต้องการวิเคราะห์

        ข้อมูลด้านบวก:
        { pos_data? }

        ข้อมูลด้านลบ:
        { neg_data? }

        คำสั่ง:
        - เขียนรายงานอย่างน้อย 350 คำ 
        -เการจัดรูปแบบ:
            1. ใช้ หัวข้อหลัก (H2) และ หัวข้อรอง (H3) ให้ชัดเจนตามโครงสร้างที่กำหนด
            2. ใช้ Bullet points หรือ Numbered lists ในส่วนของการวิเคราะห์เพื่อให้ข้อมูลอ่านง่าย ไม่เป็นก้อนข้อความ
            3. ใช้ ตัวหนา เพื่อเน้น Keyword หรือประเด็นสำคัญในแต่ละย่อหน้า
            4. ใช้ เส้นคั่นระหว่างส่วนหลักเพื่อให้รายงานดูเป็นระเบียบ
        - จัดโครงสร้างดังนี้:
            1. บทนำ
            2. วิเคราะห์ด้านบวก
            3. วิเคราะห์ด้านลบ
            4. เปรียบเทียบเชิงวิพากษ์
            5. คำวินิจฉัยเชิงประวัติศาสตร์อย่างเป็นกลาง
        - ภาษาเป็นทางการ ชัดเจน
        - ห้ามลำเอียง
        - พิมพ์ออกมาด้วย

        สุดท้าย:
        - ใช้ write_file tool
        - filename = ชื่อบุคคลหรือเหตุการณ์ที่ user ต้องการวิเคราะห์เป็นภาษาอังกฤษ
    """,
    tools=[write_file],
    generate_content_config=types.GenerateContentConfig(temperature=0),
)

# STEP 1: INQUIRY & ORCHESTRATION

court_system = SequentialAgent(
    name="court_system",
    description="กระบวนการศาลจำลองครบทุกขั้นตอน",
    sub_agents=[trial_loop, verdict_writer],
)

root_agent = Agent(
    name="inquiry_agent",
    model=Gemini(model=MODEL_NAME, retry_options=RETRY_OPTIONS),
    description="รับหัวข้อจากผู้ใช้และเริ่มกระบวนการศาลประวัติศาสตร์",
    instruction="""
        1. ถามผู้ใช้ว่าต้องการวิเคราะห์บุคคลหรือเหตุการณ์ใด
        2. เมื่อผู้ใช้ตอบ:
            - ใช้ append_to_state
            field = "PROMPT"
            - ส่งต่อไปยัง court_system
    """,
    tools=[append_to_state],
    sub_agents=[court_system],
)